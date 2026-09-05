"""learn, label and autolabel, driven against recorded history.

The history comes from the `history` fixture rather than a real recorder, but
everything downstream of it is the real code: segmenting, clustering, the
cadence, the store, and the live match that names an appliance sensor.
"""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.power_fingerprint.const import DOMAIN

CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"


def two_appliances(cycling):
    """One circuit carrying two different loads, interleaved in time.

    The second is offset by an hour so the runs alternate rather than landing
    on the same timestamps, which is how a shared circuit actually reads. Both
    sit well above the on/off threshold the trace produces, so the difference
    between them is a clustering question rather than a segmenting one.
    """
    smaller = [(stamp + timedelta(hours=1), w) for stamp, w in cycling(800.0)]
    return sorted(cycling(1200.0) + smaller)


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


async def _learn(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "learn", data, blocking=True, return_response=True
    )


def _appliance_entity(hass, entry, circuit):
    return er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_appliance_{circuit}"
    )


async def test_learning_proposes_candidates_and_describes_them(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """Clustering finds the recurring shapes and cannot name them; describing
    them in plain English is how a human recognises one to name."""
    history[CIRCUIT_A] = cycling(1200.0)
    history[CIRCUIT_B] = cycling(300.0)
    await _setup(hass, config_entry)

    response = await _learn(hass, days=7)

    found = response["circuits"][CIRCUIT_A]
    assert len(found) == 1
    assert found[0]["label"] == "unnamed_0"
    assert found[0]["runs"] == 8
    assert "peaks 1200 W" in found[0]["description"]

    store = config_entry.runtime_data.store
    assert store.unnamed()[0].cadence["runs"] == 8
    # And the candidates sensor shows them, which is the only place they are
    # visible without calling the action again.
    state = hass.states.get("sensor.power_fingerprint_unnamed_candidates")
    assert state.state == "2"
    assert state.attributes["candidates"][0]["circuit"] in (CIRCUIT_A, CIRCUIT_B)
    assert "power_fingerprint.label" in state.attributes["to_name_one"]


async def test_learning_only_the_circuits_asked_for(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    history[CIRCUIT_A] = cycling(1200.0)
    history[CIRCUIT_B] = cycling(300.0)
    await _setup(hass, config_entry)

    response = await _learn(hass, days=7, circuits=[CIRCUIT_A])
    assert list(response["circuits"]) == [CIRCUIT_A]


async def test_an_unread_circuit_says_so_rather_than_reporting_nothing_ran(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """Empty history is UNREAD, not "no appliances". The two must not look
    the same in the response."""
    history[CIRCUIT_A] = cycling(1200.0)
    await _setup(hass, config_entry)

    response = await _learn(hass, days=7)
    assert response["circuits"][CIRCUIT_B] == [{"error": "no history in window"}]


async def test_a_flat_circuit_yields_no_shapes_at_all(
    hass: HomeAssistant, config_entry, powered, history
):
    """History that was read and holds no runs is an empty list, distinct from
    the unread answer above."""
    from homeassistant.util import dt as dt_util

    start = dt_util.utcnow() - timedelta(hours=6)
    history[CIRCUIT_A] = [(start + timedelta(minutes=i), 4.0) for i in range(300)]
    await _setup(hass, config_entry)

    response = await _learn(hass, days=7)
    assert response["circuits"][CIRCUIT_A] == []


async def test_a_tighter_threshold_splits_more_shapes(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """The caller may override what the confidence setting supplies."""
    history[CIRCUIT_A] = two_appliances(cycling)
    await _setup(hass, config_entry)

    loose = await _learn(hass, days=7, threshold=5.0, circuits=[CIRCUIT_A])
    tight = await _learn(hass, days=7, threshold=0.1, circuits=[CIRCUIT_A])
    assert len(tight["circuits"][CIRCUIT_A]) > len(loose["circuits"][CIRCUIT_A])


# --- naming ----------------------------------------------------------------


async def test_naming_a_candidate_makes_it_drive_an_appliance_sensor(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """The whole contract: clustering proposes, a human names, and only then
    does anything user-facing report an appliance."""
    history[CIRCUIT_A] = cycling(1200.0)
    coordinator = await _setup(hass, config_entry)
    await _learn(hass, days=7, circuits=[CIRCUIT_A])
    assert _appliance_entity(hass, config_entry, CIRCUIT_A) is None

    await hass.services.async_call(
        DOMAIN,
        "label",
        {"circuit": CIRCUIT_A, "current_label": "unnamed_0", "new_label": "Dryer"},
        blocking=True,
    )
    # The action asks the coordinator for a refresh, which is debounced; the
    # sensor appears on the poll that follows, without a reload.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_id = _appliance_entity(hass, config_entry, CIRCUIT_A)
    assert entity_id is not None
    # Nothing is running yet, so the honest state is idle.
    assert hass.states.get(entity_id).state == "idle"

    # Three consecutive samples above the threshold identify the shape; the
    # first two are `starting`, because one reading is not a run.
    powered(mains=1300.0, a=1200.0)
    await coordinator.async_refresh()
    assert hass.states.get(entity_id).state == "starting"
    await coordinator.async_refresh()
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "Dryer"
    assert state.attributes["samples"] == 3
    assert state.attributes["circuit"] == CIRCUIT_A


async def test_a_shape_no_named_fingerprint_accounts_for_reads_unknown(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """`unknown` is a signal - something new was plugged in - not a failure."""
    history[CIRCUIT_A] = cycling(1200.0)
    history[CIRCUIT_B] = cycling(1200.0)
    coordinator = await _setup(hass, config_entry)
    await _learn(hass, days=7)
    await hass.services.async_call(
        DOMAIN,
        "label",
        {"circuit": CIRCUIT_A, "current_label": "unnamed_0", "new_label": "Dryer"},
        blocking=True,
    )
    # B has a learned candidate but no NAMED fingerprint, so nothing to match.
    powered(mains=2500.0, a=1200.0, b=1200.0)
    for _ in range(3):
        await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator.data["running"][CIRCUIT_B]["state"] == "unknown"
    assert (
        coordinator.data["running"][CIRCUIT_B]["reason"] == "no named fingerprint yet"
    )


async def test_naming_a_fingerprint_that_does_not_exist_is_refused_by_name(
    hass: HomeAssistant, config_entry, powered
):
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "label",
            {
                "circuit": CIRCUIT_A,
                "current_label": "unnamed_9",
                "new_label": "Dryer",
            },
            blocking=True,
        )
    assert err.value.translation_key == "unknown_fingerprint"
    assert err.value.translation_placeholders["label"] == "unnamed_9"


# --- autolabel -------------------------------------------------------------


async def test_a_circuit_that_already_says_what_it_is_names_itself(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """Not inference: somebody typed "Dish Washer" when they clamped the
    panel, and reading it back is free and certain."""
    hass.states.async_set(
        CIRCUIT_A,
        100.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "friendly_name": "EmporiaVue Circuit 15 Dish Washer Power",
        },
    )
    history[CIRCUIT_A] = cycling(1200.0)
    await _setup(hass, config_entry)
    await _learn(hass, days=7, circuits=[CIRCUIT_A])

    response = await hass.services.async_call(
        DOMAIN, "autolabel", {}, blocking=True, return_response=True
    )
    assert response["named"] == [{"circuit": CIRCUIT_A, "named": "Dish Washer"}]
    assert config_entry.runtime_data.store.labelled()[0].label == "Dish Washer"


async def test_a_circuit_named_only_by_number_is_left_for_a_human(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    hass.states.async_set(
        CIRCUIT_A,
        100.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "friendly_name": "EmporiaVue Circuit 25 Power",
        },
    )
    history[CIRCUIT_A] = cycling(1200.0)
    await _setup(hass, config_entry)
    await _learn(hass, days=7, circuits=[CIRCUIT_A])

    response = await hass.services.async_call(
        DOMAIN, "autolabel", {}, blocking=True, return_response=True
    )
    assert response["named"] == []
    assert response["left_for_you"] == [
        {"circuit": CIRCUIT_A, "why": "circuit has no appliance name"}
    ]


async def test_a_circuit_with_several_shapes_gets_a_suggestion_not_a_guess(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """Naming the biggest would be a guess wearing a fact's clothes."""
    hass.states.async_set(
        CIRCUIT_A,
        100.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "friendly_name": "Circuit 21 Laundry",
        },
    )
    history[CIRCUIT_A] = two_appliances(cycling)
    await _setup(hass, config_entry)
    await _learn(hass, days=7, circuits=[CIRCUIT_A], threshold=0.1)

    response = await hass.services.async_call(
        DOMAIN, "autolabel", {}, blocking=True, return_response=True
    )
    assert response["named"] == []
    skipped = response["left_for_you"][0]
    assert skipped["suggestion"] == "Laundry"
    assert "a human must say which is which" in skipped["why"]


async def test_autolabel_never_overwrites_a_name_a_person_gave(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    hass.states.async_set(
        CIRCUIT_A,
        100.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "friendly_name": "Circuit 15 Dish Washer",
        },
    )
    history[CIRCUIT_A] = cycling(1200.0)
    await _setup(hass, config_entry)
    await _learn(hass, days=7, circuits=[CIRCUIT_A])
    await hass.services.async_call(
        DOMAIN,
        "label",
        {"circuit": CIRCUIT_A, "current_label": "unnamed_0", "new_label": "Kettle"},
        blocking=True,
    )

    response = await hass.services.async_call(
        DOMAIN, "autolabel", {}, blocking=True, return_response=True
    )
    assert response["left_for_you"] == [{"circuit": CIRCUIT_A, "why": "already named"}]
    assert config_entry.runtime_data.store.labelled()[0].label == "Kettle"


async def test_the_energy_dashboards_name_beats_the_firmware_title(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """The dashboard name is what a person typed; the title is what the meter
    shipped with."""
    from types import SimpleNamespace
    from unittest.mock import patch

    hass.states.async_set(
        CIRCUIT_A,
        100.0,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "W",
            "friendly_name": "EmporiaVue Circuit 26 Power",
        },
    )
    history[CIRCUIT_A] = cycling(1200.0)
    await _setup(hass, config_entry)
    await _learn(hass, days=7, circuits=[CIRCUIT_A])

    async def _manager(hass):
        return SimpleNamespace(
            data={
                "device_consumption": [
                    {"stat_rate": CIRCUIT_A, "name": "Circuit 26 Microwave"}
                ]
            }
        )

    with patch("homeassistant.components.energy.data.async_get_manager", _manager):
        response = await hass.services.async_call(
            DOMAIN, "autolabel", {}, blocking=True, return_response=True
        )
    assert response["named"] == [{"circuit": CIRCUIT_A, "named": "Microwave"}]


async def test_a_history_row_that_is_not_a_number_is_skipped(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """`unavailable` in the middle of a trace is ordinary. A fabricated zero
    would lie about what the circuit was doing."""
    trace = cycling(1200.0)
    trace[5] = (trace[5][0], "unavailable")
    trace[6] = (trace[6][0], "unknown")
    history[CIRCUIT_A] = trace
    await _setup(hass, config_entry)

    response = await _learn(hass, days=7, circuits=[CIRCUIT_A])
    assert response["circuits"][CIRCUIT_A][0]["runs"] == 8
