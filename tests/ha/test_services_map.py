"""map_devices and apply_circuit_labels: the passive half, and what it labels.

The correlation itself is covered by the pure attribution tests; what is
covered here is everything around it - which sensors count as devices, what is
written to the store, and what a label run does to the device registry.
"""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_fingerprint.const import DOMAIN

MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"
LAMP_METER = "sensor.porch_lamp_power"
FLAT_METER = "sensor.rack_pdu_power"
PANEL_LEG = "sensor.panel_phase_a_power"

POWER = {
    "device_class": "power",
    "state_class": "measurement",
    "unit_of_measurement": "W",
}
STEP_S = 30


def square(watts, base=0.0, hours=8, on_cells=20, period=60):
    """A load switching on and off on a fixed rhythm, at the meter's cadence."""
    start = dt_util.utcnow() - timedelta(hours=hours)
    cells = int(hours * 3600 / STEP_S)
    return [
        (
            start + timedelta(seconds=STEP_S * i),
            base + (watts if i % period < on_cells else 0.0),
        )
        for i in range(cells)
    ]


@pytest.fixture
def panel(hass):
    """The meter's own device, carrying the circuits and a phase leg."""
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    meter = devices.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("demo", "panel")},
        name="Panel",
    )
    for object_id, unique in (
        ("circuit_a_power", "circuit-a"),
        ("circuit_b_power", "circuit-b"),
        ("panel_phase_a_power", "phase-a"),
    ):
        registry.async_get_or_create(
            "sensor", "demo", unique, device_id=meter.id, suggested_object_id=object_id
        )
    hass.states.async_set(PANEL_LEG, 2000.0, POWER)
    return other


@pytest.fixture
def lamp_device(hass, panel):
    """A self-metering device in an area, on someone else's config entry."""
    devices = dr.async_get(hass)
    areas = ar.async_get(hass)
    area = areas.async_get_or_create("Porch")
    device = devices.async_get_or_create(
        config_entry_id=panel.entry_id,
        identifiers={("demo", "porch-lamp")},
        name="Porch lamp",
    )
    devices.async_update_device(device.id, area_id=area.id)
    er.async_get(hass).async_get_or_create(
        "sensor",
        "demo",
        "porch-lamp-power",
        device_id=device.id,
        suggested_object_id="porch_lamp_power",
    )
    hass.states.async_set(LAMP_METER, 0.0, POWER)
    return device


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


async def _map(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "map_devices", data, blocking=True, return_response=True
    )


async def test_a_self_metering_device_is_placed_on_the_circuit_it_moves_with(
    hass: HomeAssistant, config_entry, lamp_device, powered, history
):
    lamp = square(60.0)
    history[CIRCUIT_A] = [(stamp, 500.0 + w) for stamp, w in lamp]
    history[CIRCUIT_B] = square(400.0, base=200.0, on_cells=31, period=57)
    history[LAMP_METER] = lamp
    coordinator = await _setup(hass, config_entry)

    response = await _map(hass, days=3)

    placed = {row["device"]: row for row in response["placed"]}
    assert placed[LAMP_METER]["circuit"] == CIRCUIT_A
    # The area is reported, never scored: it is what makes a wrong answer
    # visible, and it comes from the device when the entity does not carry it.
    assert placed[LAMP_METER]["area"] == "Porch"
    assert response["assignments_recorded"] == 1
    assert response["grid_source"] == "measured"
    assert response["grid_seconds"] == STEP_S
    assert response["circuits_with_history"] == 2

    row = config_entry.runtime_data.store.assignments()[LAMP_METER]
    assert row["circuit"] == CIRCUIT_A
    # Always inferred: "measured" is reserved for an active probe.
    assert row["confidence"] == "inferred"
    assert row["source"] == "correlation"
    assert row["evidence"]["grid_seconds"] == STEP_S

    # The device page gains a Circuit entity on the next poll.
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{config_entry.entry_id}_circuit_{LAMP_METER}"
    )
    assert entity_id is not None
    assert hass.states.get(entity_id).attributes["established_by"] == "correlation"


async def test_the_panel_own_sensors_are_not_treated_as_devices(
    hass: HomeAssistant, config_entry, lamp_device, powered, history
):
    """Three kinds of sensor look like devices and are not: this integration's
    own output, panel aggregates, and anything sharing a device with a
    configured circuit."""
    history[CIRCUIT_A] = [(s, 500.0 + w) for s, w in square(60.0)]
    history[LAMP_METER] = square(60.0)
    history[PANEL_LEG] = square(60.0)
    history["sensor.power_fingerprint_unmonitored_load"] = square(60.0)
    await _setup(hass, config_entry)

    response = await _map(hass, days=3)

    examined = {row["device"] for row in response["placed"] + response["unplaced"]}
    examined |= {row["device"] for row in response["no_transition_in_window"]}
    assert PANEL_LEG not in examined
    assert "sensor.power_fingerprint_unmonitored_load" not in examined
    assert MAINS not in examined
    assert CIRCUIT_B not in examined


async def test_a_device_that_never_switched_is_reported_separately(
    hass: HomeAssistant, config_entry, lamp_device, powered, history
):
    """Not "no candidate": nothing was there to look at. Six of seven rack PDU
    outlets read this way across three days."""
    hass.states.async_set(FLAT_METER, 40.0, POWER)
    history[CIRCUIT_A] = [(s, 500.0 + w) for s, w in square(60.0)]
    history[LAMP_METER] = square(60.0)
    history[FLAT_METER] = [(stamp, 40.0) for stamp, _ in square(60.0)]
    await _setup(hass, config_entry)

    response = await _map(hass, days=3)

    quiet = {row["device"] for row in response["no_transition_in_window"]}
    assert FLAT_METER in quiet
    assert FLAT_METER not in {row["device"] for row in response["placed"]}


async def test_the_grid_may_be_given_instead_of_measured(
    hass: HomeAssistant, config_entry, lamp_device, powered, history
):
    history[CIRCUIT_A] = [(s, 500.0 + w) for s, w in square(60.0)]
    history[LAMP_METER] = square(60.0)
    await _setup(hass, config_entry)

    response = await _map(hass, days=3, step=12, min_correlation=0.9)
    assert response["grid_seconds"] == 12
    assert response["grid_source"] == "given"


async def test_no_history_for_any_circuit_is_an_error_not_an_empty_result(
    hass: HomeAssistant, config_entry, powered, history
):
    """Empty history is UNREAD, not "no matches"."""
    await _setup(hass, config_entry)
    with pytest.raises(HomeAssistantError) as err:
        await _map(hass, days=3)
    assert err.value.translation_key == "no_history"
    assert err.value.translation_placeholders["count"] == "2"


# --- labelling the devices -------------------------------------------------


async def _record(entry, device_entity, circuit, source="probe"):
    await entry.runtime_data.store.async_record_assignment(
        device_entity, circuit, "measured", source, {}
    )


async def _labels(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "apply_circuit_labels", data, blocking=True, return_response=True
    )


async def test_a_label_run_defaults_to_showing_what_it_would_do(
    hass: HomeAssistant, config_entry, lamp_device, powered
):
    """Labels are the user's namespace and no integration API for them is
    documented, so the safe direction is to ask again."""
    hass.states.async_set(
        CIRCUIT_A, 100.0, {**POWER, "friendly_name": "EmporiaVue Circuit 16 Power"}
    )
    await _setup(hass, config_entry)
    await _record(config_entry, LAMP_METER, CIRCUIT_A)

    response = await _labels(hass)

    assert response["dry_run"] is True
    assert response["would_label"] == [
        {
            "device_entity": LAMP_METER,
            "device_id": lamp_device.id,
            "label": "Circuit 16",
            "established_by": "probe",
        }
    ]
    assert lr.async_get(hass).async_get_label_by_name("Circuit 16") is None


async def test_applying_adds_the_label_without_touching_the_others(
    hass: HomeAssistant, config_entry, lamp_device, powered
):
    """The other labels on that device are somebody else's."""
    labels = lr.async_get(hass)
    theirs = labels.async_create("Downstairs")
    devices = dr.async_get(hass)
    devices.async_update_device(lamp_device.id, labels={theirs.label_id})
    hass.states.async_set(
        CIRCUIT_A, 100.0, {**POWER, "friendly_name": "Circuit 16 Study"}
    )
    await _setup(hass, config_entry)
    await _record(config_entry, LAMP_METER, CIRCUIT_A)

    response = await _labels(hass, dry_run=False)

    assert response["labelled"] == 1
    ours = labels.async_get_label_by_name("Circuit 16 Study")
    assert ours is not None
    device = devices.async_get(lamp_device.id)
    assert device.labels == {theirs.label_id, ours.label_id}

    # Running it again changes nothing.
    assert (await _labels(hass, dry_run=False))["labelled"] == 0


async def test_removing_takes_only_the_circuit_labels_back_off(
    hass: HomeAssistant, config_entry, lamp_device, powered
):
    labels = lr.async_get(hass)
    theirs = labels.async_create("Downstairs")
    devices = dr.async_get(hass)
    devices.async_update_device(lamp_device.id, labels={theirs.label_id})
    hass.states.async_set(
        CIRCUIT_A, 100.0, {**POWER, "friendly_name": "Circuit 16 Study"}
    )
    await _setup(hass, config_entry)
    await _record(config_entry, LAMP_METER, CIRCUIT_A)
    await _labels(hass, dry_run=False)

    response = await _labels(hass, dry_run=False, remove=True)

    assert response["removed"] == 1
    assert devices.async_get(lamp_device.id).labels == {theirs.label_id}
    # Removing a label that is not there is not an error.
    assert (await _labels(hass, dry_run=False, remove=True))["removed"] == 0


async def test_the_prefix_is_only_added_where_the_name_lacks_one(
    hass: HomeAssistant, config_entry, lamp_device, powered
):
    """Meter firmware titles get trimmed; a name somebody typed is used whole."""
    hass.states.async_set(
        CIRCUIT_A,
        100.0,
        {**POWER, "friendly_name": "EmporiaVue Circuit 15 Dish Washer Power"},
    )
    await _setup(hass, config_entry)
    await _record(config_entry, LAMP_METER, CIRCUIT_A)

    response = await _labels(hass, prefix="Breaker")
    assert response["would_label"][0]["label"] == "Breaker Circuit 15 Dish Washer"


async def test_an_assignment_to_a_device_that_has_gone_is_skipped(
    hass: HomeAssistant, config_entry, powered
):
    """A device removed from the registry between the probe and the label run
    is ordinary; nothing to label, nothing to report."""
    await _setup(hass, config_entry)
    await _record(config_entry, "sensor.long_gone", CIRCUIT_A)
    response = await _labels(hass, dry_run=False)
    assert response["labelled"] == 0
    assert response["devices"] == []


async def test_a_power_sensor_with_no_state_class_is_not_a_device(
    hass: HomeAssistant, config_entry, lamp_device, powered, history
):
    """A `device_class: power` sensor that is not a measurement is a total or
    a forecast, and correlating it against a circuit means nothing."""
    hass.states.async_set(
        "sensor.tariff_forecast_power", 100.0, {"device_class": "power"}
    )
    history[CIRCUIT_A] = [(s, 500.0 + w) for s, w in square(60.0)]
    history[LAMP_METER] = square(60.0)
    history["sensor.tariff_forecast_power"] = square(60.0)
    await _setup(hass, config_entry)

    response = await _map(hass, days=3)

    every = (
        response["placed"] + response["unplaced"] + response["no_transition_in_window"]
    )
    assert "sensor.tariff_forecast_power" not in {row["device"] for row in every}


async def test_a_device_that_switches_on_its_own_rhythm_is_left_unplaced(
    hass: HomeAssistant, config_entry, lamp_device, powered, history
):
    """It transitioned, so it is not "nothing to look at" - it simply does not
    match a circuit, and unplaced is a different answer."""
    devices = dr.async_get(hass)
    stray = devices.async_get_or_create(
        config_entry_id=lamp_device.config_entries.copy().pop(),
        identifiers={("demo", "rack-pdu")},
        name="Rack PDU",
    )
    er.async_get(hass).async_get_or_create(
        "sensor",
        "demo",
        "rack-pdu-power",
        device_id=stray.id,
        suggested_object_id="rack_pdu_power",
    )
    hass.states.async_set(FLAT_METER, 40.0, POWER)
    history[CIRCUIT_A] = [(s, 500.0 + w) for s, w in square(60.0)]
    history[LAMP_METER] = square(60.0)
    history[FLAT_METER] = square(40.0, on_cells=7, period=23)
    await _setup(hass, config_entry)

    response = await _map(hass, days=3)

    unplaced = {row["device"]: row for row in response["unplaced"]}
    assert FLAT_METER in unplaced
    # No area on that device, and none is invented for the report.
    assert unplaced[FLAT_METER]["area"] is None


async def test_a_stored_assignment_with_no_circuit_labels_nothing(
    hass: HomeAssistant, config_entry, lamp_device, powered, hass_storage
):
    """A half-written row cannot name a breaker, and must not name an empty one."""
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": {
            "fingerprints": [],
            "assignments": {LAMP_METER: {"circuit": "", "source": "correlation"}},
        },
    }
    await _setup(hass, config_entry)
    assert (await _labels(hass))["would_label"] == []


async def test_a_circuit_with_no_name_of_its_own_falls_back_to_the_prefix(
    hass: HomeAssistant, config_entry, lamp_device, powered
):
    """A meter that ships no friendly name still produces a usable label
    rather than an empty one."""
    await _setup(hass, config_entry)
    await _record(config_entry, LAMP_METER, CIRCUIT_A)

    response = await _labels(hass)
    assert response["would_label"][0]["label"] == "Circuit"


async def test_an_assignment_to_an_entity_outside_the_registry_makes_no_entity(
    hass: HomeAssistant, config_entry, lamp_device, powered
):
    """Nothing to attach a Circuit entity to, so none is created - and the
    listener must not raise on the way to deciding that."""
    coordinator = await _setup(hass, config_entry)
    await _record(config_entry, "sensor.long_gone", CIRCUIT_A)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert (
        er.async_get(hass).async_get_entity_id(
            "sensor", DOMAIN, f"{config_entry.entry_id}_circuit_sensor.long_gone"
        )
        is None
    )


async def test_a_device_that_left_the_registry_between_runs_is_skipped(
    hass: HomeAssistant, config_entry, lamp_device, powered
):
    """A device deleted after it was mapped takes its entities with it, so the
    stored assignment names nothing. The run skips it and labels the rest."""
    hass.states.async_set(
        CIRCUIT_A, 100.0, {**POWER, "friendly_name": "Circuit 16 Study"}
    )
    await _setup(hass, config_entry)
    await _record(config_entry, LAMP_METER, CIRCUIT_A)
    dr.async_get(hass).async_remove_device(lamp_device.id)

    response = await _labels(hass, dry_run=False)
    assert response["labelled"] == 0
