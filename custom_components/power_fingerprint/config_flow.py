"""Config, reconfigure and options flow.

Setup asks for nothing to be typed. The mains and circuits are pre-selected
from the energy dashboard, or from the mains sensor's own device; the tuning
waits in a collapsed section; the price is asked last, and only when the
energy dashboard has none.
"""

from __future__ import annotations

from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .analysis import to_watts
from .const import (
    CONF_BREAKER_PROBE,
    CONF_CIRCUITS,
    CONF_CONFIDENCE,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    CONFIDENCE_PROFILES,
    DEFAULT_BREAKER_PROBE,
    DEFAULT_CONFIDENCE,
    DEFAULT_PRICE,
    DEFAULT_TOLERANCE,
    DOMAIN,
    SECTION_ADVANCED,
)
from .dashboard import async_dashboard_power_sensors, async_dashboard_price

_POWER_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power")
)
_POWER_SENSORS = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
)
_TUNING_DEFAULTS: dict[str, Any] = {
    CONF_TOLERANCE: DEFAULT_TOLERANCE,
    CONF_CONFIDENCE: DEFAULT_CONFIDENCE,
    CONF_BREAKER_PROBE: DEFAULT_BREAKER_PROBE,
    CONF_PAIRS: "",
}


async def _defaults_with_price(
    hass: HomeAssistant, defaults: dict[str, Any]
) -> dict[str, Any]:
    """Fill in the price from the energy dashboard when the user has none set.

    Only ever a default. A price the user has already chosen wins over the
    dashboard's, because the dashboard changing should not silently rewrite
    what someone deliberately configured here.
    """
    if defaults.get(CONF_PRICE) is not None:
        return defaults
    price = await async_dashboard_price(hass)
    if price is None:
        return defaults
    return {**defaults, CONF_PRICE: price}


def _sensor_fields(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    # A default of None reaches the frontend as null, passes its required check
    # and comes back as a selector error; with no guess the field opens empty.
    mains = defaults.get(CONF_MAINS)
    return {
        (
            vol.Required(CONF_MAINS)
            if mains is None
            else vol.Required(CONF_MAINS, default=mains)
        ): _POWER_SENSOR,
        vol.Required(
            CONF_CIRCUITS, default=defaults.get(CONF_CIRCUITS, [])
        ): _POWER_SENSORS,
    }


def _price_field(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    return {
        vol.Required(
            CONF_PRICE, default=defaults.get(CONF_PRICE, DEFAULT_PRICE)
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, max=5, step=0.001, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }


def _tuning_fields(defaults: dict[str, Any]) -> dict[vol.Marker, Any]:
    values = {**_TUNING_DEFAULTS, **defaults}
    return {
        vol.Optional(
            CONF_TOLERANCE, default=values[CONF_TOLERANCE]
        ): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=50, step=0.5, mode=selector.NumberSelectorMode.BOX
            )
        ),
        # One dial the user can reason about. The individual
        # thresholds are not exposed: almost nobody has 27 real clamps to
        # check answers against, so numeric knobs would be controls with no
        # feedback. What a person CAN say is how they would rather be wrong.
        vol.Optional(
            CONF_CONFIDENCE, default=values[CONF_CONFIDENCE]
        ): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=list(CONFIDENCE_PROFILES),
                translation_key="confidence",
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Optional(
            CONF_BREAKER_PROBE, default=values[CONF_BREAKER_PROBE]
        ): selector.BooleanSelector(),
        # One `switch.entity: sensor.circuit_power` per line. Kept as free
        # text because a config flow has no native pair-list selector, and
        # this is easier to review than a nested repeating form.
        vol.Optional(CONF_PAIRS, default=values[CONF_PAIRS]): selector.TextSelector(
            selector.TextSelectorConfig(multiline=True)
        ),
    }


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    """Every field on one form, for Reconfigure and Configure."""
    return vol.Schema(
        {
            **_sensor_fields(defaults),
            **_price_field(defaults),
            **_tuning_fields(defaults),
        }
    )


def _setup_schema(defaults: dict[str, Any], collapsed: bool) -> vol.Schema:
    """The sensors, with the tuning in a collapsed section; the price comes last."""
    # No default on the section key: the frontend would send that default as
    # the section's data and never fill in the defaults of the fields inside.
    return vol.Schema(
        {
            **_sensor_fields(defaults),
            vol.Required(SECTION_ADVANCED): section(
                vol.Schema(_tuning_fields({})), {"collapsed": collapsed}
            ),
        }
    )


def _unreadable(
    hass: HomeAssistant, entity: str, kind: str
) -> tuple[str, dict[str, str]] | None:
    """Why a sensor cannot be read as power now, as an error key and placeholders."""
    state = hass.states.get(entity)
    if state is None:
        return f"{kind}_missing", {"entity": entity}
    try:
        value = float(state.state)
    except TypeError, ValueError:
        return f"{kind}_not_numeric", {"entity": entity}
    unit = state.attributes.get("unit_of_measurement")
    if to_watts(value, unit) is None:
        return "not_power_unit", {"entity": entity, "unit": str(unit)}
    return None


def _watts(hass: HomeAssistant, entity: str) -> float | None:
    """A readable power sensor's value in watts, else None."""
    state = hass.states.get(entity)
    if state is None or _unreadable(hass, entity, "circuit") is not None:
        return None
    return to_watts(float(state.state), state.attributes.get("unit_of_measurement"))


def _power_sensors(hass: HomeAssistant) -> set[str]:
    return {
        state.entity_id
        for state in hass.states.async_all("sensor")
        if state.attributes.get("device_class") == "power"
    }


def _validate(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Prove the chosen sensors can actually be read, before accepting them.

    THE SELECTOR IS NOT A CHECK. It filters on `device_class: power`, which
    constrains neither the unit nor whether the sensor currently reports a
    number - and both of those failures are silent afterwards. A kilowatt
    sensor produces thresholds a thousand times too high and every circuit
    reports zero runs forever; a sensor stuck on `unknown` produces a device
    full of blank entities. Catching them here means the user is told at the
    moment they can still pick a different sensor.

    Returns (errors, placeholders) so the caller can render the message with
    the offending entity named rather than a generic failure. The error key
    says which field failed: a circuit refused with a message about "the
    mains reading" sent people to fix the wrong sensor.
    """
    mains = user_input[CONF_MAINS]
    circuits = list(user_input.get(CONF_CIRCUITS) or [])

    if not circuits:
        return {CONF_CIRCUITS: "no_circuits"}, {}
    if mains in circuits:
        return {CONF_CIRCUITS: "mains_in_circuits"}, {}

    for field, entity in [(CONF_MAINS, mains), *((CONF_CIRCUITS, c) for c in circuits)]:
        problem = _unreadable(
            hass, entity, "mains" if field == CONF_MAINS else "circuit"
        )
        if problem is not None:
            return {field: problem[0]}, problem[1]
    return {}, {}


def _device_circuits(hass: HomeAssistant, mains: str) -> list[str]:
    """The other power sensors on the mains sensor's device, if they add up to it.

    A meter that also reports its phases or a total would count them twice,
    and a second meter's circuits would be missing; either way the sum misses
    the mains reading, and then nothing is guessed.
    """
    registry = er.async_get(hass)
    entry = registry.async_get(mains)
    mains_w = _watts(hass, mains)
    if entry is None or entry.device_id is None or not mains_w:
        return []
    power = _power_sensors(hass)
    readings = {
        e.entity_id: _watts(hass, e.entity_id)
        for e in er.async_entries_for_device(registry, entry.device_id)
        if e.entity_id != mains and e.entity_id in power
    }
    circuits = sorted(e for e, w in readings.items() if w is not None)
    total = sum(w for w in readings.values() if w is not None)
    if abs(total - mains_w) > abs(mains_w) * DEFAULT_TOLERANCE / 100:
        return []
    return circuits


async def _suggested_sensors(hass: HomeAssistant) -> dict[str, Any]:
    """The mains and circuits a new user most likely means, where that can be told."""
    grid, devices = await async_dashboard_power_sensors(hass)
    mains = next((e for e in grid if _watts(hass, e) is not None), None)
    circuits = [e for e in devices if e != mains and _watts(hass, e) is not None]
    if mains is not None and not circuits:
        circuits = _device_circuits(hass, mains)
    return {CONF_MAINS: mains, CONF_CIRCUITS: circuits}


class PowerFingerprintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup and reconfiguration."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    @override
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if len(_power_sensors(self.hass)) < 2:
            return await self.async_step_no_sensors()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            data = {
                **_TUNING_DEFAULTS,
                **{k: v for k, v in user_input.items() if k != SECTION_ADVANCED},
                **(user_input.get(SECTION_ADVANCED) or {}),
            }
            errors, placeholders = _validate(self.hass, data)
            if not errors:
                self._data = data
                return await self.async_step_price()
        schema = _setup_schema(
            await _suggested_sensors(self.hass), collapsed=not errors
        )
        if user_input is not None:
            schema = self.add_suggested_values_to_schema(schema, user_input)
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_no_sensors(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """No power sensors to read yet: say what provides them, check on Submit."""
        if user_input is not None and len(_power_sensors(self.hass)) >= 2:
            return await self.async_step_user()
        return self.async_show_form(
            step_id="no_sensors",
            errors={"base": "still_no_sensors"} if user_input is not None else {},
            description_placeholders={"found": str(len(_power_sensors(self.hass)))},
        )

    async def async_step_price(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """The price per kWh, asked only when the energy dashboard has none."""
        if user_input is None:
            price = await async_dashboard_price(self.hass)
            if price is not None:
                user_input = {CONF_PRICE: price}
        if user_input is not None:
            return self.async_create_entry(
                title="Power Fingerprint",
                data={**self._data, CONF_PRICE: float(user_input[CONF_PRICE])},
            )
        return self.async_show_form(
            step_id="price", data_schema=vol.Schema(_price_field({}))
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the panel wiring without deleting and re-adding the entry.

        The options flow can edit the same fields, but only reconfigure can be
        reached from the entry's own menu, and only reconfigure survives the
        entry being renamed or moved. Re-clamping a panel is exactly the kind
        of change this exists for.
        """
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            errors, placeholders = _validate(self.hass, user_input)
            if not errors:
                # options={} on purpose: the runtime merge lets entry.options
                # win, so options saved once would silently override every
                # later reconfigure. The form was seeded from the merged view
                # and carries every field, so clearing options loses nothing.
                return self.async_update_reload_and_abort(
                    entry, data=user_input, options={}
                )
        merged = {**entry.data, **entry.options, **(user_input or {})}
        merged = await _defaults_with_price(self.hass, merged)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_schema(merged),
            errors=errors,
            description_placeholders=placeholders,
        )

    @staticmethod
    @callback
    @override
    def async_get_options_flow(entry: ConfigEntry) -> PowerFingerprintOptionsFlow:
        return PowerFingerprintOptionsFlow()


class PowerFingerprintOptionsFlow(OptionsFlow):
    """Let the circuit list, price and pairs be edited after setup."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            errors, placeholders = _validate(self.hass, user_input)
            if not errors:
                return self.async_create_entry(title="", data=user_input)
        merged = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            merged.update(user_input)
        merged = await _defaults_with_price(self.hass, merged)
        return self.async_show_form(
            step_id="init",
            data_schema=_schema(merged),
            errors=errors,
            description_placeholders=placeholders,
        )
