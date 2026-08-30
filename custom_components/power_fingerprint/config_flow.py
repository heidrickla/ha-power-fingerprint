"""Config and options flow."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_CIRCUITS,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    DEFAULT_PRICE,
    DEFAULT_TOLERANCE,
    DOMAIN,
)

_POWER_SENSOR = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power")
)
_POWER_SENSORS = selector.EntitySelector(
    selector.EntitySelectorConfig(domain="sensor", device_class="power", multiple=True)
)


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
                selector.NumberSelectorConfig(min=0, max=5, step=0.001, mode="box")
            ),
            vol.Optional(
                CONF_TOLERANCE,
                default=defaults.get(CONF_TOLERANCE, DEFAULT_TOLERANCE),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=1, max=50, step=0.5, mode="box")
            ),
            # One `switch.entity: sensor.circuit_power` per line. Kept as free
            # text because a config flow has no native pair-list selector, and
            # this is easier to review than a nested repeating form.
            vol.Optional(
                CONF_PAIRS, default=defaults.get(CONF_PAIRS, "")
            ): selector.TextSelector(selector.TextSelectorConfig(multiline=True)),
        }
    )


class PowerFingerprintConfigFlow(ConfigFlow, domain=DOMAIN):
    """Initial setup."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> FlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Power Fingerprint", data=user_input)
        return self.async_show_form(step_id="user", data_schema=_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(entry: ConfigEntry) -> OptionsFlow:
        return PowerFingerprintOptionsFlow()


class PowerFingerprintOptionsFlow(OptionsFlow):
    """Let the circuit list, price and pairs be edited after setup."""

    async def async_step_init(self, user_input: dict | None = None) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        merged = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(step_id="init", data_schema=_schema(merged))
