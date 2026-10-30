"""Config and options flow."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_fingerprint.const import (
    CONF_BREAKER_PROBE,
    CONF_CIRCUITS,
    CONF_CONFIDENCE,
    CONF_MAINS,
    CONF_PAIRS,
    CONF_PRICE,
    CONF_TOLERANCE,
    DOMAIN,
    SECTION_ADVANCED,
)

POWER_ATTRS = {
    "device_class": "power",
    "state_class": "measurement",
    "unit_of_measurement": "W",
}

# Kept local rather than imported from conftest, so tests/ need not be a
# package - the pure-module tests deliberately load their targets by path.
MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"

# Reconfigure and Configure show every field on one form.
USER_INPUT = {
    CONF_MAINS: MAINS,
    CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B],
    CONF_PRICE: 0.13,
    CONF_TOLERANCE: 5.0,
    CONF_PAIRS: "",
}

# Setup's first screen: the sensors, and the tuning in the Advanced section.
SETUP_INPUT = {
    CONF_MAINS: MAINS,
    CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B],
    SECTION_ADVANCED: {CONF_TOLERANCE: 5.0, CONF_PAIRS: ""},
}

MANAGER = "homeassistant.components.energy.data.async_get_manager"


def energy(prefs):
    """The energy manager, carrying the dashboard preferences in `data`."""

    async def _get(hass):
        return SimpleNamespace(data=prefs)

    return patch(MANAGER, _get)


async def _start(hass: HomeAssistant):
    return await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )


async def _price(hass: HomeAssistant, result, answer=None):
    """The last step, shown when the energy dashboard has no price."""
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "price"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], answer or {}
    )
    await hass.async_block_till_done()
    return result


def _section(result):
    """The Advanced section on the setup form."""
    for marker, value in result["data_schema"].schema.items():
        if marker == SECTION_ADVANCED:
            return value
    raise AssertionError("no Advanced section on the form")


async def test_user_flow_creates_an_entry(hass: HomeAssistant, powered):
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert list(result["data_schema"].schema) == [
        CONF_MAINS,
        CONF_CIRCUITS,
        SECTION_ADVANCED,
    ]
    assert _section(result).options == {"collapsed": True}

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], SETUP_INPUT
    )
    result = await _price(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_MAINS: MAINS,
        CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B],
        CONF_PRICE: 0.13,
        CONF_TOLERANCE: 5.0,
        CONF_CONFIDENCE: "balanced",
        CONF_BREAKER_PROBE: True,
        CONF_PAIRS: "",
    }


async def test_the_first_screen_submitted_untouched_creates_the_entry(
    hass: HomeAssistant, powered
):
    """The energy dashboard names the sensors and the price: one Submit."""
    prefs = {
        "energy_sources": [
            {"type": "grid", "stat_rate": MAINS, "number_energy_price": 0.145}
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.a_energy", "stat_rate": CIRCUIT_A},
            {"stat_consumption": "sensor.b_energy", "stat_rate": CIRCUIT_B},
        ],
    }
    with energy(prefs):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        CONF_MAINS: MAINS,
        CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B],
        CONF_PRICE: 0.145,
        CONF_TOLERANCE: 5.0,
        CONF_CONFIDENCE: "balanced",
        CONF_BREAKER_PROBE: True,
        CONF_PAIRS: "",
    }


async def test_the_advanced_section_is_stored_flat(hass: HomeAssistant, powered):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            **SETUP_INPUT,
            SECTION_ADVANCED: {
                CONF_TOLERANCE: 8.0,
                CONF_CONFIDENCE: "cautious",
                CONF_BREAKER_PROBE: False,
                CONF_PAIRS: f"switch.lamp: {CIRCUIT_A}",
            },
        },
    )
    result = await _price(hass, result, {CONF_PRICE: 0.27})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert SECTION_ADVANCED not in result["data"]
    assert result["data"][CONF_TOLERANCE] == 8.0
    assert result["data"][CONF_CONFIDENCE] == "cautious"
    assert result["data"][CONF_BREAKER_PROBE] is False
    assert result["data"][CONF_PAIRS] == f"switch.lamp: {CIRCUIT_A}"
    assert result["data"][CONF_PRICE] == 0.27


async def test_an_error_opens_the_section_and_keeps_what_was_typed(
    hass: HomeAssistant, powered
):
    advanced = {CONF_TOLERANCE: 9.0, CONF_PAIRS: "switch.lamp: sensor.x"}
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**SETUP_INPUT, CONF_MAINS: "sensor.gone", SECTION_ADVANCED: advanced},
    )
    assert result["errors"] == {CONF_MAINS: "mains_missing"}
    section = _section(result)
    assert section.options == {"collapsed": False}
    suggested = {
        str(marker): (marker.description or {}).get("suggested_value")
        for marker in section.schema.schema
    }
    assert suggested[CONF_TOLERANCE] == 9.0
    assert suggested[CONF_PAIRS] == "switch.lamp: sensor.x"


async def test_without_power_sensors_the_flow_says_how_and_checks_again(
    hass: HomeAssistant, powered
):
    for entity in (MAINS, CIRCUIT_A, CIRCUIT_B):
        hass.states.async_remove(entity)
    hass.states.async_set(MAINS, 155.0, {"device_class": "power"})
    result = await _start(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "no_sensors"
    assert result["errors"] == {}
    assert result["description_placeholders"] == {"found": "1"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "no_sensors"
    assert result["errors"] == {"base": "still_no_sensors"}

    powered()
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    hass.config_entries.flow.async_abort(result["flow_id"])


# --- what the first screen pre-selects -------------------------------------


def _default(result, key):
    """The default the form offers for one field."""
    for marker in result["data_schema"].schema:
        if marker == key:
            return marker.default()
    raise AssertionError(f"{key} is not on the form")


async def test_the_energy_dashboard_names_the_mains_and_circuits(
    hass: HomeAssistant, powered
):
    """A plug inside a circuit, and a sensor that cannot be read, are left out."""
    hass.states.async_set("sensor.dead_power", "unavailable", {"device_class": "power"})
    prefs = {
        "energy_sources": [{"type": "grid", "power": [{"stat_rate": MAINS}]}],
        "device_consumption": [
            {"stat_consumption": "sensor.a_energy", "stat_rate": CIRCUIT_A},
            {
                "stat_consumption": "sensor.lamp_energy",
                "stat_rate": CIRCUIT_B,
                "included_in_stat": "sensor.a_energy",
            },
            {"stat_consumption": "sensor.x_energy", "stat_rate": "sensor.dead_power"},
        ],
    }
    with energy(prefs):
        result = await _start(hass)
    assert _default(result, CONF_MAINS) == MAINS
    assert _default(result, CONF_CIRCUITS) == [CIRCUIT_A]


async def test_with_solar_the_grid_is_not_offered_as_the_mains(
    hass: HomeAssistant, powered
):
    """The grid reads the panel less what the panels make, so it is not the mains."""
    prefs = {
        "energy_sources": [
            {"type": "grid", "stat_rate": MAINS},
            {"type": "solar", "stat_energy_from": "sensor.solar_energy"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.a_energy", "stat_rate": CIRCUIT_A},
        ],
    }
    with energy(prefs):
        result = await _start(hass)
    assert _default(result, CONF_MAINS) is None
    assert _default(result, CONF_CIRCUITS) == [CIRCUIT_A]


def _one_meter(hass: HomeAssistant, *names: str) -> None:
    """Power sensors on one meter device, as an energy monitor's integration makes."""
    entry = MockConfigEntry(domain="meter")
    entry.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("meter", "panel")}
    )
    for name in names:
        # The registry will not hand out an id a state already holds; the
        # readings go back once the entity is registered under that id.
        state = hass.states.get(f"sensor.{name}")
        hass.states.async_remove(f"sensor.{name}")
        registered = er.async_get(hass).async_get_or_create(
            "sensor",
            "meter",
            name,
            device_id=device.id,
            config_entry=entry,
            suggested_object_id=name,
        )
        assert registered.entity_id == f"sensor.{name}"
        hass.states.async_set(registered.entity_id, state.state, state.attributes)


async def test_the_mains_device_supplies_circuits_that_add_up_to_it(
    hass: HomeAssistant, powered
):
    _one_meter(hass, "mains_power", "circuit_a_power", "circuit_b_power")
    with energy({"energy_sources": [{"type": "grid", "stat_rate": MAINS}]}):
        result = await _start(hass)
    assert _default(result, CONF_MAINS) == MAINS
    assert _default(result, CONF_CIRCUITS) == [CIRCUIT_A, CIRCUIT_B]


async def test_a_meter_that_also_reads_its_phases_is_not_guessed(
    hass: HomeAssistant, powered
):
    """The phases would count every circuit twice; the sum shows it."""
    hass.states.async_set(
        "sensor.phase_a_power",
        155.0,
        {"device_class": "power", "unit_of_measurement": "W"},
    )
    _one_meter(
        hass, "mains_power", "circuit_a_power", "circuit_b_power", "phase_a_power"
    )
    with energy({"energy_sources": [{"type": "grid", "stat_rate": MAINS}]}):
        result = await _start(hass)
    assert _default(result, CONF_MAINS) == MAINS
    assert _default(result, CONF_CIRCUITS) == []


async def test_a_mains_with_no_device_is_offered_alone(hass: HomeAssistant, powered):
    with energy({"energy_sources": [{"type": "grid", "stat_rate": MAINS}]}):
        result = await _start(hass)
    assert _default(result, CONF_MAINS) == MAINS
    assert _default(result, CONF_CIRCUITS) == []


async def test_nothing_is_pre_selected_without_an_energy_dashboard(
    hass: HomeAssistant, powered
):
    result = await _start(hass)
    assert _default(result, CONF_MAINS) is None
    assert _default(result, CONF_CIRCUITS) == []


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


async def _recovers(hass: HomeAssistant, result):
    """Submit good input on the form an error left behind; it must create."""
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], SETUP_INPUT
    )
    result = await _price(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_CIRCUITS] == [CIRCUIT_A, CIRCUIT_B]


async def test_a_sensor_that_does_not_exist_is_refused_by_name(
    hass: HomeAssistant, powered
):
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SETUP_INPUT, CONF_MAINS: "sensor.not_a_thing"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAINS: "mains_missing"}
    assert result["description_placeholders"]["entity"] == "sensor.not_a_thing"
    await _recovers(hass, result)


async def test_a_missing_circuit_is_refused_as_a_circuit(hass: HomeAssistant, powered):
    """Not as the mains: the message has to send people to the right sensor."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SETUP_INPUT, CONF_CIRCUITS: [CIRCUIT_A, "sensor.gone"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CIRCUITS: "circuit_missing"}
    assert result["description_placeholders"]["entity"] == "sensor.gone"
    await _recovers(hass, result)


@pytest.mark.parametrize("stuck", ["unknown", "unavailable", "not a number"])
async def test_a_mains_not_reporting_a_number_is_refused(
    hass: HomeAssistant, powered, stuck
):
    """The selector filters on device_class; it cannot see the current value."""
    hass.states.async_set(MAINS, stuck, POWER_ATTRS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], SETUP_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAINS: "mains_not_numeric"}
    assert result["description_placeholders"]["entity"] == MAINS
    powered()
    await _recovers(hass, result)


async def test_a_circuit_not_reporting_a_number_is_refused_as_a_circuit(
    hass: HomeAssistant, powered
):
    hass.states.async_set(CIRCUIT_B, "unknown", POWER_ATTRS)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], SETUP_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CIRCUITS: "circuit_not_numeric"}
    assert result["description_placeholders"]["entity"] == CIRCUIT_B
    powered()
    await _recovers(hass, result)


async def test_an_empty_circuit_list_is_refused(hass: HomeAssistant, powered):
    """Nothing to compare the mains against, so every derived figure is empty."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SETUP_INPUT, CONF_CIRCUITS: []}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CIRCUITS: "no_circuits"}
    await _recovers(hass, result)


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
        result["flow_id"], SETUP_INPUT
    )
    result = await _price(hass, result)
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
        result["flow_id"], SETUP_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_MAINS: "not_power_unit"}
    assert result["description_placeholders"]["unit"] == "A"
    powered()
    await _recovers(hass, result)


async def test_the_mains_may_not_also_be_a_circuit(hass: HomeAssistant, powered):
    """Otherwise it is counted on both sides and coverage can never balance."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**SETUP_INPUT, CONF_CIRCUITS: [MAINS, CIRCUIT_A]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CIRCUITS: "mains_in_circuits"}
    await _recovers(hass, result)


# --- the energy dashboard's price ------------------------------------------


async def test_the_dashboards_price_is_used_without_asking(
    hass: HomeAssistant, powered
):
    """One place to keep the tariff right, and it is the dashboard's."""
    with patch(
        "custom_components.power_fingerprint.config_flow.async_dashboard_price",
        return_value=0.145,
    ):
        result = await _start(hass)
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], SETUP_INPUT
        )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PRICE] == 0.145


async def test_a_price_already_chosen_beats_the_dashboards(
    hass: HomeAssistant, config_entry, powered
):
    """The dashboard changing must not silently rewrite a deliberate setting."""
    config_entry.add_to_hass(hass)
    with patch(
        "custom_components.power_fingerprint.config_flow.async_dashboard_price",
        return_value=0.145,
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN,
            context={
                "source": config_entries.SOURCE_RECONFIGURE,
                "entry_id": config_entry.entry_id,
            },
        )
    assert result["type"] is FlowResultType.FORM
    assert _default(result, CONF_PRICE) == 0.13


@pytest.mark.parametrize(("dashboard", "offered"), [(0.145, 0.145), (None, 0.13)])
async def test_an_entry_without_a_price_is_offered_the_dashboards(
    hass: HomeAssistant, powered, dashboard, offered
):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DOMAIN,
        data={CONF_MAINS: MAINS, CONF_CIRCUITS: [CIRCUIT_A, CIRCUIT_B]},
    )
    entry.add_to_hass(hass)
    with patch(
        "custom_components.power_fingerprint.config_flow.async_dashboard_price",
        return_value=dashboard,
    ):
        result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert _default(result, CONF_PRICE) == offered


async def test_without_a_dashboard_price_the_last_step_offers_the_constant(
    hass: HomeAssistant, powered
):
    result = await _start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], SETUP_INPUT
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "price"
    assert _default(result, CONF_PRICE) == 0.13
    result = await _price(hass, result)
    assert result["data"][CONF_PRICE] == 0.13


# --- options flow errors ---------------------------------------------------


async def test_options_refuse_a_broken_sensor_then_accept_a_good_one(
    hass: HomeAssistant, config_entry, powered
):
    """The options flow runs the same validation and recovers the same way."""
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_CIRCUITS: [CIRCUIT_A, "sensor.gone"]}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_CIRCUITS: "circuit_missing"}
    assert result["description_placeholders"]["entity"] == "sensor.gone"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_PRICE: 0.21}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options[CONF_PRICE] == 0.21


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

    # The same form, corrected, goes through.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_CIRCUITS: [CIRCUIT_A]}
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_CIRCUITS] == [CIRCUIT_A]


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
    # The abort reloads the entry. Let the reload finish, or its coordinator
    # schedules a refresh timer after shutdown that nothing cancels.
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    # The reconfigured values are effective, not shadowed.
    assert config_entry.data[CONF_CIRCUITS] == [CIRCUIT_A]
    assert config_entry.data[CONF_PRICE] == 0.13
    assert not config_entry.options
