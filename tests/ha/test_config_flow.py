"""Config and options flow."""

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.power_fingerprint.const import (
    CONF_CIRCUITS,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    DOMAIN,
)

# Kept local rather than imported from conftest, so tests/ need not be a
# package - the pure-module tests deliberately load their targets by path.
MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"

USER_INPUT = {
    CONF_MAINS: MAINS,
    CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B],
    CONF_PRICE: 0.13,
    CONF_TOLERANCE: 5.0,
    CONF_PAIRS: "",
}


async def test_user_flow_creates_an_entry(hass: HomeAssistant, powered):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_MAINS] == MAINS
    assert result["data"][CONF_CIRCUITS] == [CIRCUIT_A, CIRCUIT_B]


async def test_only_one_instance_is_allowed(hass: HomeAssistant, config_entry, powered):
    """A second instance would double-count every circuit."""
    config_entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_options_flow_updates_the_running_entry(
    hass: HomeAssistant, config_entry, powered
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_PRICE: 0.21}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_PRICE] == 0.21


async def test_contradiction_pairs_survive_the_flow(
    hass: HomeAssistant, config_entry, powered
):
    """Free text, because a config flow has no pair-list widget - so it is worth
    proving it round-trips rather than assuming."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {**USER_INPUT, CONF_PAIRS: f"switch.lamp: {CIRCUIT_A}"},
    )
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    assert coordinator.pairs == [("switch.lamp", CIRCUIT_A)]
