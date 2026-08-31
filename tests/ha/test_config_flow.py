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

    coordinator = config_entry.runtime_data.coordinator
    assert coordinator.pairs == [("switch.lamp", CIRCUIT_A)]


# --- test-before-configure -------------------------------------------------
#
# The entity selector filters on device_class only. Everything below passes
# that filter and would still be a broken configuration, silently, afterwards.


async def test_a_sensor_that_does_not_exist_is_refused_by_name(
    hass: HomeAssistant, powered
):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_MAINS: "sensor.not_a_thing"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAINS: "mains_missing"}
    assert result["description_placeholders"]["entity"] == "sensor.not_a_thing"


async def test_a_kilowatt_sensor_is_accepted(hass: HomeAssistant, powered):
    """kW is a power unit, so it configures fine - it is converted, not refused."""
    hass.states.async_set(
        MAINS,
        0.155,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_a_non_power_unit_is_refused_naming_the_unit(
    hass: HomeAssistant, powered
):
    hass.states.async_set(
        MAINS,
        12.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "A",
        },
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], USER_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAINS: "not_power_unit"}
    assert result["description_placeholders"]["unit"] == "A"


async def test_the_mains_may_not_also_be_a_circuit(hass: HomeAssistant, powered):
    """Otherwise it is counted on both sides and coverage can never balance."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_CIRCUITS: [MAINS, CIRCUIT_A]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CIRCUITS: "mains_in_circuits"}


# --- reconfiguration-flow --------------------------------------------------


async def test_reconfigure_replaces_the_circuit_list(
    hass: HomeAssistant, config_entry, powered
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": config_entry.entry_id,
        },
    )
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_CIRCUITS: [CIRCUIT_A]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_CIRCUITS] == [CIRCUIT_A]


async def test_reconfigure_refuses_a_broken_sensor_and_keeps_the_old_one(
    hass: HomeAssistant, config_entry, powered
):
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": config_entries.SOURCE_RECONFIGURE,
            "entry_id": config_entry.entry_id,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_MAINS: "sensor.gone"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAINS: "mains_missing"}
    assert config_entry.data[CONF_MAINS] == MAINS


async def test_reconfigure_is_not_shadowed_by_previously_saved_options(
    hass, config_entry, powered
):
    """Options saved once used to override every later reconfigure silently:
    the runtime merge lets entry.options win over entry.data."""
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_MAINS: MAINS,
            CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B],
            CONF_PRICE: 0.31,
            CONF_TOLERANCE: 5.0,
            CONF_PAIRS: "",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_PRICE] == 0.31

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_MAINS: MAINS,
            CONF_CIRCUITS: [CIRCUIT_A],
            CONF_PRICE: 0.13,
            CONF_TOLERANCE: 5.0,
            CONF_PAIRS: "",
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # The reconfigured values are effective, not shadowed.
    assert config_entry.data[CONF_CIRCUITS] == [CIRCUIT_A]
    assert config_entry.data[CONF_PRICE] == 0.13
    assert not config_entry.options
