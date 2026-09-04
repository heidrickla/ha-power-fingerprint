"""Config, reconfigure and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector

from .analysis import to_watts
from .const import (
    CONF_CIRCUITS,
    CONF_CONFIDENCE,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    CONFIDENCE_PROFILES,
    DEFAULT_CONFIDENCE,
    DEFAULT_PRICE,
    DEFAULT_TOLERANCE,
    DOMAIN,
)
from .dashboard import async_dashboard_price

_POWER_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power")
)
_POWER_SENSORS = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
)


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


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_MAINS, default=defaults.get(CONF_MAINS)): _POWER_SENSOR,
            vol.Required(
                CONF_CIRCUITS, default=defaults.get(CONF_CIRCUITS, [])
            ): _POWER_SENSORS,
            vol.Optional(
                CONF_PRICE, default=defaults.get(CONF_PRICE, DEFAULT_PRICE)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=0, max=5, step=0.001, mode=selector.NumberSelectorMode.BOX
                )
            ),
            vol.Optional(
                CONF_TOLERANCE,
                default=defaults.get(CONF_TOLERANCE, DEFAULT_TOLERANCE),
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
                CONF_CONFIDENCE,
                default=defaults.get(CONF_CONFIDENCE, DEFAULT_CONFIDENCE),
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(CONFIDENCE_PROFILES),
                    translation_key="confidence",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            # One `switch.entity: sensor.circuit_power` per line. Kept as free
            # text because a config flow has no native pair-list selector, and
            # this is easier to review than a nested repeating form.
            vol.Optional(
                CONF_PAIRS, default=defaults.get(CONF_PAIRS, "")
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        }
    )


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
        kind = "mains" if field == CONF_MAINS else "circuit"
        state = hass.states.get(entity)
        if state is None:
            return {field: f"{kind}_missing"}, {"entity": entity}
        if state.state in ("unknown", "unavailable", ""):
            return {field: f"{kind}_not_numeric"}, {"entity": entity}
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return {field: f"{kind}_not_numeric"}, {"entity": entity}
        unit = state.attributes.get("unit_of_measurement")
        if to_watts(value, unit) is None:
            return {field: "not_power_unit"}, {
                "entity": entity,
                "unit": str(unit),
            }
    return {}, {}


# `domain=` is a real keyword on Home Assistant's ConfigFlow.__init_subclass__.
# It only looks wrong when HA is not installed and the base class degrades to
# `object`, which is the state a workstation lint runs in.
class PowerFingerprintConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]
    """Initial setup and reconfiguration."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        placeholders: dict[str, str] = {}
        if user_input is not None:
            errors, placeholders = _validate(self.hass, user_input)
            if not errors:
                return self.async_create_entry(
                    title="Power Fingerprint", data=user_input
                )
        seed = await _defaults_with_price(self.hass, user_input or {})
        return self.async_show_form(
            step_id="user",
            data_schema=_schema(seed),
            errors=errors,
            description_placeholders=placeholders,
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
