"""correlate and set_device_power, driven against recorded history.

Shapes are learned from the `history` fixture's traces; the entities lined up
against them are real registry entries on real devices, with their state
changes answered by the same fixture.
"""

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.power_fingerprint.const import DOMAIN

CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"
SWITCH = "switch.garage_heater"
METER = "sensor.garage_heater_power"
MOTION = "binary_sensor.hall_motion"
TV = "media_player.den_tv"
POWER_ATTRS = {
    "device_class": "power",
    "state_class": "measurement",
    "unit_of_measurement": "W",
}


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data


async def _call(hass, service, **data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


async def _set_power(hass, **data):
    await hass.services.async_call(DOMAIN, "set_device_power", data, blocking=True)


def _device(hass, name, model, entities):
    """A device from another integration, with registry rows for `entities`."""
    other = MockConfigEntry(domain="acme")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("acme", name)},
        name=name,
        manufacturer="Acme",
        model=model,
    )
    registry = er.async_get(hass)
    for entity_id, (state, attrs) in entities.items():
        domain, object_id = entity_id.split(".")
        row = registry.async_get_or_create(
            domain,
            "acme",
            object_id,
            suggested_object_id=object_id,
            device_id=device.id,
            config_entry=other,
            original_device_class=attrs.get("device_class"),
        )
        assert row.entity_id == entity_id
        hass.states.async_set(entity_id, state, attrs)
    return device.id


def _edges(rows, idle=2.0):
    """Where each run in a `cycling` trace starts and its last loaded reading."""
    starts, ends, prev = [], [], idle
    last_on = None
    for stamp, watts in rows:
        if prev <= idle < watts:
            starts.append(stamp)
        if watts <= idle < prev:
            ends.append(last_on)
        if watts > idle:
            last_on = stamp
        prev = watts
    return starts, ends


def _toggles(starts, ends, on="on", off="off", lead=-5, lag=10):
    first = starts[0] - timedelta(hours=1)
    rows = [(first, off)]
    for s, e in zip(starts, ends, strict=True):
        rows.append((s + timedelta(seconds=lead), on))
        rows.append((e + timedelta(seconds=lag), off))
    return rows


async def _learned(hass, config_entry, history, cycling):
    history[CIRCUIT_A] = cycling(1200.0)
    runtime = await _setup(hass, config_entry)
    await _call(hass, "learn", circuits=[CIRCUIT_A])
    return runtime, *_edges(history[CIRCUIT_A])


async def test_the_switch_is_suggested_and_the_motion_sensor_is_context(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    heater = _device(
        hass,
        "Garage Heater",
        "HX-1500",
        {SWITCH: ("off", {}), METER: ("0", POWER_ATTRS)},
    )
    _device(hass, "Hall Motion", "PIR-2", {MOTION: ("off", {"device_class": "motion"})})
    history[SWITCH] = _toggles(starts, ends)
    history[METER] = _toggles(starts, ends, on=1195.0, off=0.0)
    history[MOTION] = _toggles(starts, ends, lead=3, lag=60)

    response = await _call(hass, "correlate")

    rows = response["circuits"][CIRCUIT_A]
    assert [r["label"] for r in rows] == ["unnamed_0"]
    shape = rows[0]
    assert shape["runs"] == len(starts)
    assert shape["draw_w"]["mean"] == pytest.approx(1198.0, abs=5)
    assert shape["hint"] == "heating element or motor likelier"

    top = shape["suggestions"][0]
    assert top["device"]["id"] == heater
    assert top["device"]["manufacturer"] == "Acme"
    assert top["device"]["model"] == "HX-1500"
    assert top["entity_id"] in (SWITCH, METER)
    assert top["role"] in ("switch", "meter")
    assert top["verdict"] == "clear"
    assert top["measured_w"] == pytest.approx(1195.0)
    assert top["step_w"] == pytest.approx(1198.0, abs=5)
    assert top["fit"] == "fits"

    assert MOTION not in {s["entity_id"] for s in shape["suggestions"]}
    motion = [c for c in shape["context"] if c["entity_id"] == MOTION]
    assert motion
    assert motion[0]["role"] == "context"
    assert motion[0]["fit"] == "not_a_load"
    assert response["sources"]["series"] >= 4


async def test_a_rating_rules_out_a_device_too_small_for_the_shape(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    runtime, starts, ends = await _learned(hass, config_entry, history, cycling)
    tv = _device(hass, "Den TV", "QN-65", {TV: ("off", {})})
    history[TV] = _toggles(starts, ends)

    await _set_power(
        hass,
        device_id=tv,
        kind="load",
        max_w=400,
        typical_w=120,
        source="spec sheet",
    )
    assert runtime.store.device_power()[tv] == {
        "kind": "load",
        "max_w": 400.0,
        "typical_w": 120.0,
        "source": "spec sheet",
    }

    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    row = next(s for s in shape["suggestions"] if s["entity_id"] == TV)
    assert row["role"] == "load"
    assert row["rating"]["max_w"] == 400.0
    assert row["fit"] == "too_small"

    await _set_power(hass, device_id=tv, remove=True)
    assert tv not in runtime.store.device_power()
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    row = next(s for s in shape["suggestions"] if s["entity_id"] == TV)
    assert row["rating"] is None
    # With no rating, the circuit's step at the TV's own events decides.
    assert row["fit"] == "fits"


async def test_a_stored_kind_overrides_the_guess_from_the_domain(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    lamp = _device(hass, "Hall Lamp", "L-1", {"light.hall_lamp": ("off", {})})
    history["light.hall_lamp"] = _toggles(starts, ends)
    await _set_power(hass, device_id=lamp, kind="sensor")

    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    assert "light.hall_lamp" not in {s["entity_id"] for s in shape["suggestions"]}
    assert "light.hall_lamp" in {c["entity_id"] for c in shape["context"]}


async def test_named_shapes_are_left_out_unless_asked_for(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    await _learned(hass, config_entry, history, cycling)
    await hass.services.async_call(
        DOMAIN,
        "label",
        {"circuit": CIRCUIT_A, "current_label": "unnamed_0", "new_label": "Heater"},
        blocking=True,
    )
    assert (await _call(hass, "correlate"))["circuits"][CIRCUIT_A] == []
    rows = (await _call(hass, "correlate", include_named=True))["circuits"][CIRCUIT_A]
    assert [r["label"] for r in rows] == ["Heater"]


async def test_an_unread_circuit_says_so(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    await _learned(hass, config_entry, history, cycling)
    response = await _call(hass, "correlate", circuits=[CIRCUIT_B])
    assert response["circuits"][CIRCUIT_B] == [{"error": "no history in window"}]


async def test_thermostat_action_changes_are_a_series(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    _device(hass, "Thermostat", "T-9", {"climate.house": ("cool", {})})
    history["climate.house"] = [
        (stamp, "cool", {"hvac_action": action})
        for stamp, action in _toggles(starts, ends, on="cooling", off="idle")
    ]
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    row = next(
        s
        for s in shape["suggestions"]
        if (s["entity_id"], s["transition"]) == ("climate.house", "hvac_action cooling")
    )
    assert row["role"] == "switch"
    # Its reports run on the thermostat's own schedule; the step does not judge it.
    assert row["fit"] == "unknown"


async def test_a_whole_house_meter_is_set_aside_as_upstream(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    house = "sensor.battery_power_to_home"
    _device(hass, "Home Battery", "B-10", {house: ("900", POWER_ATTRS)})
    history[house] = _toggles(starts, ends, on=5200.0, off=900.0)
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    assert house not in {s["entity_id"] for s in shape["suggestions"]}
    upstream = [c for c in shape["context"] if c["entity_id"] == house]
    assert upstream
    assert upstream[0]["fit"] == "upstream"


async def test_a_meter_is_read_over_the_runs_its_device_lined_up_with(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """The heater accounts for three of the shape's eight runs; its meter reads
    nothing during the other five, which are another load's."""
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    heater = _device(
        hass,
        "Garage Heater",
        "HX-1500",
        {SWITCH: ("off", {}), METER: ("0", POWER_ATTRS)},
    )
    mine = [1, 4, 6]
    history[SWITCH] = _toggles([starts[i] for i in mine], [ends[i] for i in mine])
    history[METER] = _toggles(
        [starts[i] for i in mine], [ends[i] for i in mine], on=1195.0, off=0.0
    )
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    row = next(s for s in shape["suggestions"] if s["device"]["id"] == heater)
    assert row["matched"] == 3
    assert row["measured_w"] == pytest.approx(1195.0)
    assert row["fit"] == "fits"


async def test_a_switch_on_a_device_with_several_meters_takes_none_of_them(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    outlet_1 = "sensor.strip_outlet_1_power"
    outlet_2 = "sensor.strip_outlet_2_power"
    strip = _device(
        hass,
        "Power Strip",
        "PS-6",
        {
            "switch.strip_outlet_2": ("off", {}),
            outlet_1: ("0", POWER_ATTRS),
            outlet_2: ("0", POWER_ATTRS),
        },
    )
    history["switch.strip_outlet_2"] = _toggles(starts, ends)
    history[outlet_2] = _toggles(starts, ends, on=1195.0, off=0.0)
    history[outlet_1] = _toggles(starts, ends, on=0.5, off=0.0, lead=900, lag=900)
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    rows = [s for s in shape["suggestions"] if (s["device"] or {}).get("id") == strip]
    assert rows
    for row in rows:
        if row["entity_id"] == "switch.strip_outlet_2":
            assert row["measured_w"] is None
        assert row["fit"] == "fits"


async def test_a_motion_sensor_on_the_lights_device_stays_in_context(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    light = "light.garage_motion_light"
    motion = "binary_sensor.garage_motion_light_motion"
    _device(
        hass,
        "Garage Motion Light",
        "26931",
        {light: ("off", {}), motion: ("off", {"device_class": "motion"})},
    )
    history[light] = _toggles(starts, ends)
    history[motion] = _toggles(starts, ends)
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    assert light in {s["entity_id"] for s in shape["suggestions"]}
    assert motion in {c["entity_id"] for c in shape["context"]}


async def test_a_numeric_sensor_without_a_unit_is_not_a_series(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    counter = "sensor.hall_counter"
    hass.states.async_set(counter, "20", {})
    history[counter] = _toggles(starts, ends, on="10", off="20")
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    listed = {r["entity_id"] for r in (*shape["suggestions"], *shape["context"])}
    assert counter not in listed


async def test_a_run_still_going_at_the_last_reading_has_no_end(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    _, starts, ends = await _learned(hass, config_entry, history, cycling)
    # The last run's trailing idle readings had not arrived yet.
    history[CIRCUIT_A] = history[CIRCUIT_A][:-4]
    _device(hass, "Garage Heater", "HX-1500", {SWITCH: ("off", {})})
    history[SWITCH] = _toggles(starts, ends)
    shape = (await _call(hass, "correlate"))["circuits"][CIRCUIT_A][0]
    rows = {s["edge"]: s for s in shape["suggestions"] if s["entity_id"] == SWITCH}
    assert rows["start"]["runs"] == len(starts)
    assert rows["end"]["runs"] == len(ends) - 1
    assert rows["end"]["matched"] == len(ends) - 1


async def test_setting_power_for_a_device_that_does_not_exist_is_refused(
    hass: HomeAssistant, config_entry, powered, history
):
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "set_device_power",
            {"device_id": "nope", "kind": "load"},
            blocking=True,
        )
    assert err.value.translation_key == "unknown_device"


async def test_setting_power_without_a_kind_is_refused(
    hass: HomeAssistant, config_entry, powered, history
):
    await _setup(hass, config_entry)
    device = _device(hass, "Thing", "X", {"switch.thing": ("off", {})})
    with pytest.raises(ServiceValidationError) as err:
        await hass.services.async_call(
            DOMAIN,
            "set_device_power",
            {"device_id": device, "max_w": 100},
            blocking=True,
        )
    assert err.value.translation_key == "kind_required"


async def test_correlate_refuses_while_unloaded(
    hass: HomeAssistant, config_entry, powered
):
    runtime = await _setup(hass, config_entry)
    assert runtime is not None
    await hass.config_entries.async_unload(config_entry.entry_id)
    with pytest.raises(ServiceValidationError) as err:
        await _call(hass, "correlate")
    assert err.value.translation_key == "not_loaded"
