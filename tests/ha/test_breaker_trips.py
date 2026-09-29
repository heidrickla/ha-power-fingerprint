"""Breaker trips detected live: what is recorded, fired and reported, and when not.

The detection rule is covered against recorded states in
`tests/test_breaker_replay.py`; what is covered here is the Home Assistant
side - which entities are fed to it, arming after startup, eligibility from
the recorder's statistics, and what a trip writes.
"""

import json
import math
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_capture_events,
    async_fire_time_changed,
)

from custom_components.power_fingerprint.breaker import GRACE_S, STABLE_S
from custom_components.power_fingerprint.const import DOMAIN, EVENT_BREAKER_TRIP

MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"
PANEL_LEG = "sensor.panel_phase_a_power"
LAMP_METER = "sensor.lamp_power"
TV = ("media_player.tv", "remote.tv")
BED = ("binary_sensor.bed_occupied", "light.bed_light")
BED_BUTTON = "button.bed_calibrate"
UNMONITORED = "sensor.power_fingerprint_unmonitored_load"

POWER = {
    "device_class": "power",
    "state_class": "measurement",
    "unit_of_measurement": "W",
}


@pytest.fixture
def house(hass):
    """The meter's own device, and a TV, a bed and a metered lamp fed by A."""
    other = MockConfigEntry(domain="demo")
    other.add_to_hass(hass)
    devices = dr.async_get(hass)
    registry = er.async_get(hass)
    meter = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("demo", "panel")}, name="Panel"
    )
    for object_id in ("circuit_a_power", "circuit_b_power", "panel_phase_a_power"):
        registry.async_get_or_create(
            "sensor",
            "demo",
            object_id,
            device_id=meter.id,
            suggested_object_id=object_id,
        )
    made = {}
    for name, entities in (
        ("tv", [("media_player", "tv"), ("remote", "tv")]),
        (
            "bed",
            [
                ("binary_sensor", "bed_occupied"),
                ("light", "bed_light"),
                ("button", "bed_calibrate"),
            ],
        ),
        ("lamp", [("switch", "lamp"), ("sensor", "lamp_power")]),
    ):
        device = devices.async_get_or_create(
            config_entry_id=other.entry_id,
            identifiers={("demo", name)},
            name=name.title(),
        )
        made[name] = device
        for domain, object_id in entities:
            registry.async_get_or_create(
                domain,
                "demo",
                f"{name}-{object_id}",
                device_id=device.id,
                suggested_object_id=object_id,
                original_device_class="power" if object_id == "lamp_power" else None,
            )
    for entity, state in (
        ("media_player.tv", "on"),
        ("remote.tv", "on"),
        ("binary_sensor.bed_occupied", "off"),
        ("light.bed_light", "off"),
        (BED_BUTTON, "unknown"),
        ("switch.lamp", "on"),
    ):
        hass.states.async_set(entity, state)
    hass.states.async_set(LAMP_METER, 40.0, POWER)
    hass.states.async_set(PANEL_LEG, 2000.0, POWER)
    return made


def _hours(low, hours=200, idle_hour=None):
    top = math.floor(dt_util.utcnow().timestamp() / 3600.0) * 3600.0
    rows = [{"start": top - (i + 1) * 3600.0, "min": low} for i in range(hours)]
    if idle_hour is not None:
        rows[idle_hour]["min"] = 0.3
    return rows


class _Stats(dict):
    calls = 0


@pytest.fixture
def stats(history):
    """Answer the recorder's hourly statistics from a table the test fills."""
    table = _Stats()

    def _during(hass, start, end, ids, period, units, types):
        assert period == "hour"
        assert types == {"min"}
        assert units == {"power": "W"}
        table.calls += 1
        return {i: [dict(row) for row in table[i]] for i in ids if i in table}

    with patch(
        "homeassistant.components.recorder.statistics.statistics_during_period",
        _during,
    ):
        top = math.floor(dt_util.utcnow().timestamp() / 3600.0) * 3600.0
        # An hour with no minimum, and one older than the lookback at 0 W:
        # neither may count against A.
        table[CIRCUIT_A] = [
            *_hours(90.0),
            {"start": top - 300 * 3600.0, "min": None},
            {"start": top - 800 * 3600.0, "min": 0.0},
        ]
        # B reached the noise floor once: an appliance circuit.
        table[CIRCUIT_B] = _hours(40.0, idle_hour=30)
        yield table


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    return entry.runtime_data.coordinator.breaker


async def _tick(hass, freezer, seconds):
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done(wait_background_tasks=True)


async def _arm(hass, freezer):
    await _tick(hass, freezer, STABLE_S + 1)


async def _flip(hass, freezer, casualties=(), lamp_to=None, also=()):
    """A's breaker off for about 10 s, with casualties shortly after."""
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    for circuit in also:
        hass.states.async_set(circuit, 0.0, POWER)
    await _tick(hass, freezer, 2)
    for entity in casualties:
        hass.states.async_set(entity, "unavailable")
    if lamp_to is not None:
        hass.states.async_set(LAMP_METER, lamp_to, POWER)
    await _tick(hass, freezer, 4)
    hass.states.async_set(CIRCUIT_A, 0.1, POWER)
    await _tick(hass, freezer, 6)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    for circuit in also:
        hass.states.async_set(circuit, 50.0, POWER)
    await _tick(hass, freezer, GRACE_S + 1)


# --- a trip -----------------------------------------------------------------


async def test_a_trip_is_recorded_once_per_device_and_fired(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)

    await _flip(hass, freezer, casualties=[*TV, *BED], lamp_to=0.0)

    [event] = events
    assert event.data["circuit"] == CIRCUIT_A
    assert event.data["start"] < event.data["end"]
    assert event.data["confirmed"] == [LAMP_METER]
    assert event.data["suspected"] == sorted([*TV, *BED])

    rows = watch._store.assignments()
    # One row per device: the TV's two entities and the bed's two share one.
    assert sorted(rows) == sorted(
        ["binary_sensor.bed_occupied", LAMP_METER, "media_player.tv"]
    )
    for row in rows.values():
        assert row["circuit"] == CIRCUIT_A
        assert row["source"] == "breaker"
        assert row["confidence"] == "inferred"
        assert row["evidence"]["start"] == event.data["start"]
        assert row["evidence"]["end"] == event.data["end"]
    assert rows[LAMP_METER]["evidence"]["result"] == "confirmed"
    assert rows["media_player.tv"]["evidence"]["result"] == "suspected"
    assert rows["media_player.tv"]["evidence"]["entities"] == sorted(TV)

    registry = er.async_get(hass)
    for device in house.values():
        ours = [
            row
            for row in er.async_entries_for_device(registry, device.id)
            if row.platform == DOMAIN
        ]
        assert len(ours) == 1, device.name


async def test_the_last_trip_and_the_watched_circuits_are_on_the_sensor(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    await _setup(hass, config_entry)
    before = hass.states.get(UNMONITORED).attributes
    assert before["breaker_trip_circuits"] == [CIRCUIT_A]
    assert before["last_breaker_trip"] is None

    await _arm(hass, freezer)
    await _flip(hass, freezer, casualties=TV)

    trip = hass.states.get(UNMONITORED).attributes["last_breaker_trip"]
    assert trip["circuit"] == CIRCUIT_A
    assert trip["suspected"] == sorted(TV)
    assert trip["confirmed"] == []


async def test_diagnostics_list_watched_circuits_and_the_trip_without_ids(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    from custom_components.power_fingerprint.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _setup(hass, config_entry)
    await _arm(hass, freezer)
    await _flip(hass, freezer, casualties=TV)

    report = await async_get_config_entry_diagnostics(hass, config_entry)
    breaker = report["breaker"]
    assert breaker["floor_w"] == 4.0
    assert breaker["armed_at"] is not None
    assert len(breaker["eligible"]) == 1
    [watched] = breaker["eligible"]
    assert breaker["circuits"][watched]["eligible"] is True
    assert breaker["circuits"][watched]["floor_w"] == 90.0
    others = [v for k, v in breaker["circuits"].items() if k != watched]
    assert others[0]["eligible"] is False
    assert others[0]["ever_seen_idle"] is True
    assert breaker["last_trip"]["circuit"] == watched
    assert breaker["last_trip"]["suspected"] == 2
    dumped = json.dumps(report)
    for entity in (CIRCUIT_A, CIRCUIT_B, *TV):
        assert entity not in dumped


# --- what is never written --------------------------------------------------


async def test_a_probe_answer_is_never_overwritten_by_a_trip(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    watch = await _setup(hass, config_entry)
    await watch._store.async_record_assignment(
        "media_player.tv", CIRCUIT_B, "measured", "probe", {"probes": 3}
    )
    await _arm(hass, freezer)
    await _flip(hass, freezer, casualties=TV)

    row = watch._store.assignments()["media_player.tv"]
    assert row["source"] == "probe"
    assert row["circuit"] == CIRCUIT_B


async def test_the_meters_own_sensors_and_this_integrations_are_not_casualties(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    await _setup(hass, config_entry)
    await _arm(hass, freezer)
    own = "sensor.power_fingerprint_standby_power"
    assert hass.states.get(own) is not None

    await _flip(hass, freezer, casualties=[PANEL_LEG, MAINS, own, *TV])

    [event] = events
    assert event.data["suspected"] == sorted(TV)


async def test_an_entity_going_unknown_is_not_a_casualty(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    """A button reads `unknown` until pressed; that is not losing power."""
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    await _setup(hass, config_entry)
    hass.states.async_set(BED_BUTTON, "2026-09-28T00:00:00+00:00")
    await _arm(hass, freezer)
    # A new reading, so the button's press is outside the stable period.
    hass.states.async_set(CIRCUIT_A, 101.0, POWER)
    await _tick(hass, freezer, 6)

    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 3)
    hass.states.async_set(BED_BUTTON, "unknown")
    hass.states.async_set("media_player.tv", "unavailable")
    await _tick(hass, freezer, 6)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, GRACE_S + 1)

    [event] = events
    assert event.data["suspected"] == ["media_player.tv"]


async def test_two_circuits_dead_together_fire_nothing(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    from custom_components.power_fingerprint.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)

    await _flip(hass, freezer, casualties=TV, also=[CIRCUIT_B])

    assert events == []
    assert watch._store.assignments() == {}
    assert watch.last_refusal["kind"] == "together"
    assert watch.last_refusal["together"] == [CIRCUIT_B]
    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert report["breaker"]["last_refusal"]["kind"] == "together"
    assert report["breaker"]["last_trip"] is None


# --- when nothing is detected -----------------------------------------------


async def test_nothing_is_detected_in_the_stable_period_after_setup(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    assert watch.armed_at == watch.listening_since + timedelta(seconds=STABLE_S)

    await _tick(hass, freezer, 60)
    await _flip(hass, freezer, casualties=TV)

    assert events == []
    assert watch._store.assignments() == {}


async def test_arming_waits_for_home_assistant_to_start(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    hass.set_state(CoreState.starting)
    watch = await _setup(hass, config_entry)
    assert watch.armed_at is None
    assert watch.detector.armed_from == math.inf

    await _tick(hass, freezer, 30)
    hass.set_state(CoreState.running)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()

    assert watch.armed_at == dt_util.utcnow() + timedelta(seconds=STABLE_S)


async def test_a_circuit_ineligible_on_its_history_is_not_watched(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    assert watch.eligible == [CIRCUIT_A]
    assert not watch.verdicts[CIRCUIT_B].eligible
    await _arm(hass, freezer)

    hass.states.async_set(CIRCUIT_B, 0.0, POWER)
    hass.states.async_set("media_player.tv", "unavailable")
    await _tick(hass, freezer, 12)
    hass.states.async_set(CIRCUIT_B, 50.0, POWER)
    await _tick(hass, freezer, GRACE_S + 1)

    assert events == []


async def test_a_trip_does_not_cost_its_circuit_eligibility(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    """The live hour now holds a zero; the recorded trip covers it."""
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    await _flip(hass, freezer, casualties=TV)

    assert min(watch._hourly[CIRCUIT_A].values()) == 0.0
    await watch.async_refresh_eligibility()
    assert watch.eligible == [CIRCUIT_A]


async def test_without_statistics_no_circuit_is_watched(
    hass: HomeAssistant, config_entry, house, powered, caplog
):
    watch = await _setup(hass, config_entry)
    assert watch.eligible == []
    assert all(not v.eligible for v in watch.verdicts.values())
    assert "breaker trips are detected only" in caplog.text


async def test_unloading_stops_listening(
    hass: HomeAssistant, config_entry, house, powered, stats
):
    watch = await _setup(hass, config_entry)
    before = len(watch.detector._circuit_log[CIRCUIT_A])
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await hass.async_block_till_done()
    assert len(watch.detector._circuit_log[CIRCUIT_A]) == before


# --- longer outages, and the housekeeping around them -------------------------


async def test_a_trip_still_dead_at_the_cap_is_fired_without_an_end(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    """Known while the breaker is still off. Entities with no device are keyed
    by themselves, and the attribute lists are capped."""
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    loose = [f"switch.loose_{n:02d}" for n in range(30)]
    for entity in loose:
        hass.states.async_set(entity, "on")
    await _arm(hass, freezer)

    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 2)
    for entity in loose:
        hass.states.async_set(entity, "unavailable")
    await _tick(hass, freezer, 125)

    [event] = events
    assert event.data["end"] is None
    assert event.data["suspected"] == loose
    assert sorted(watch._store.assignments()) == loose
    shown = hass.states.get(UNMONITORED).attributes["last_breaker_trip"]
    assert len(shown["suspected"]) == 25
    assert shown["suspected_not_shown"] == 5
    # The open trip covers the hours up to now, so A stays watched.
    assert watch.eligible == [CIRCUIT_A]

    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, GRACE_S + 1)
    assert len(events) == 1
    [trip] = watch._store.breaker_trips()
    assert trip["end"] is not None
    assert trip["end"] > trip["start"]


async def test_a_circuit_dying_again_before_the_close_is_one_trip(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    await _setup(hass, config_entry)
    await _arm(hass, freezer)

    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 3)
    hass.states.async_set("media_player.tv", "unavailable")
    await _tick(hass, freezer, 3)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, 10)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, GRACE_S)
    assert events == []

    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, GRACE_S + 1)
    [event] = events
    assert event.data["suspected"] == ["media_player.tv"]


async def test_eligibility_is_read_again_every_hour(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    """B's only idle hour is at the far edge of the lookback, and ages out."""
    stats[CIRCUIT_B] = _hours(40.0, hours=720, idle_hour=718)
    watch = await _setup(hass, config_entry)
    assert stats.calls == 1
    assert watch.eligible == [CIRCUIT_A]

    await _tick(hass, freezer, 3601)

    assert stats.calls == 2
    assert watch.eligible == [CIRCUIT_A, CIRCUIT_B]


async def test_a_device_gone_while_its_circuit_still_reported_live_is_not_counted(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    """An unchanged reading is reported without a state change. The circuit's
    last live reading is its last report, and the TV went before it."""
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    await _setup(hass, config_entry)
    await _arm(hass, freezer)

    hass.states.async_set("media_player.tv", "unavailable")
    await _tick(hass, freezer, 10)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)  # re-reported, unchanged
    await _tick(hass, freezer, 6)
    await _flip(hass, freezer, casualties=["remote.tv"])

    [event] = events
    assert event.data["suspected"] == ["remote.tv"]


@pytest.mark.parametrize("gap", ["unavailable", "unknown", "nan"])
async def test_a_zero_after_an_unreadable_reading_opens_nothing(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer, gap
):
    """A circuit sensor's first reading after a restart has nothing before it,
    even when it is zero. `nan` parses as a number and is refused as one."""
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)

    hass.states.async_set(CIRCUIT_A, gap, POWER)
    await _tick(hass, freezer, 3)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    hass.states.async_set("media_player.tv", "unavailable")
    await _tick(hass, freezer, 6)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, GRACE_S + 1)

    assert events == []
    assert watch.detector.open_circuit is None
    assert watch.last_refusal is None


async def test_a_removed_entity_is_not_a_casualty(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer, caplog
):
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    await _setup(hass, config_entry)
    await _arm(hass, freezer)

    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 3)
    hass.states.async_remove("remote.tv")
    hass.states.async_set("media_player.tv", "unavailable")
    await _tick(hass, freezer, 3)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    await _tick(hass, freezer, GRACE_S + 1)

    [event] = events
    assert event.data["suspected"] == ["media_player.tv"]
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_a_renamed_circuit_keeps_its_trips(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    await _flip(hass, freezer, casualties=TV)

    await watch._store.async_migrate_entity_id(CIRCUIT_A, "sensor.circuit_c_power")

    [trip] = watch._store.breaker_trips()
    assert trip["circuit"] == "sensor.circuit_c_power"


async def test_diagnostics_without_a_watch_report_nothing_for_it(
    hass: HomeAssistant, config_entry, house, powered, stats
):
    from custom_components.power_fingerprint.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    await _setup(hass, config_entry)
    config_entry.runtime_data.coordinator.breaker = None
    report = await async_get_config_entry_diagnostics(hass, config_entry)
    assert report["breaker"] == {}


# --- what keeps a circuit watched, and where rows go --------------------------


async def test_a_trip_that_dropped_nothing_counts_against_its_circuit(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    """What an appliance switching off looks like: it fires once, then the
    circuit is judged to idle at the floor and is no longer watched."""
    events = async_capture_events(hass, EVENT_BREAKER_TRIP)
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)

    await _flip(hass, freezer)

    [event] = events
    assert event.data["confirmed"] == []
    assert event.data["suspected"] == []
    assert watch.eligible == []
    assert "reached 0.0 W" in watch.verdicts[CIRCUIT_A].reason


async def test_a_trip_still_open_across_a_reload_ends_at_the_next_live_reading(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)
    hass.states.async_set(CIRCUIT_A, 0.0, POWER)
    await _tick(hass, freezer, 2)
    hass.states.async_set("media_player.tv", "unavailable")
    await _tick(hass, freezer, 125)
    assert watch._store.breaker_trips()[0]["end"] is None

    assert await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    watch = config_entry.runtime_data.coordinator.breaker
    await _tick(hass, freezer, 10)
    hass.states.async_set(CIRCUIT_A, 100.0, POWER)
    back = dt_util.utcnow().timestamp()
    await _tick(hass, freezer, 1)

    [trip] = watch._store.breaker_trips()
    assert abs(dt_util.parse_datetime(trip["end"]).timestamp() - back) < 0.01
    assert watch.eligible == [CIRCUIT_A]


async def test_the_row_goes_where_the_device_already_has_one(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    """A passive placement keyed on the bed's light takes the trip's answer,
    so the bed keeps one Circuit entity."""
    watch = await _setup(hass, config_entry)
    await watch._store.async_record_assignment(
        "light.bed_light", CIRCUIT_B, "inferred", "correlation", {}
    )
    await _arm(hass, freezer)

    await _flip(hass, freezer, casualties=BED)

    rows = watch._store.assignments()
    assert sorted(rows) == ["light.bed_light"]
    assert rows["light.bed_light"]["source"] == "breaker"
    assert rows["light.bed_light"]["circuit"] == CIRCUIT_A


async def test_the_row_goes_on_the_devices_own_power_sensor(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer
):
    watch = await _setup(hass, config_entry)
    await _arm(hass, freezer)

    await _flip(hass, freezer, casualties=["switch.lamp"])

    rows = watch._store.assignments()
    assert sorted(rows) == [LAMP_METER]
    assert rows[LAMP_METER]["evidence"]["entities"] == ["switch.lamp"]


async def test_a_statistics_failure_is_logged_once_and_its_recovery_once(
    hass: HomeAssistant, config_entry, house, powered, stats, freezer, caplog
):
    watch = await _setup(hass, config_entry)
    assert watch.eligible == [CIRCUIT_A]

    def _broken(*_args):
        raise RuntimeError("database is locked")

    with patch(
        "homeassistant.components.recorder.statistics.statistics_during_period",
        _broken,
    ):
        await _tick(hass, freezer, 3601)
        await _tick(hass, freezer, 3601)
    assert caplog.text.count("Could not read circuit statistics") == 1
    assert caplog.text.count("readable again") == 0

    await _tick(hass, freezer, 3601)
    assert caplog.text.count("readable again") == 1
    assert watch.eligible == [CIRCUIT_A]


async def test_a_circuit_without_statistics_says_so(
    hass: HomeAssistant, config_entry, house, powered, stats
):
    del stats[CIRCUIT_B]
    watch = await _setup(hass, config_entry)
    assert "no long-term statistics" in watch.verdicts[CIRCUIT_B].reason
    assert "no long-term statistics" not in watch.verdicts[CIRCUIT_A].reason


async def test_the_readings_at_setup_count_toward_their_hour(
    hass: HomeAssistant, config_entry, house, powered, stats
):
    watch = await _setup(hass, config_entry)
    assert 100.0 in watch._hourly[CIRCUIT_A].values()
    assert 50.0 in watch._hourly[CIRCUIT_B].values()
