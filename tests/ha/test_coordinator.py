"""The poll loop: the window it keeps, and what it refuses to conclude from it."""

import sys
from datetime import timedelta
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.power_fingerprint.const import DOMAIN, POLL_SECONDS
from custom_components.power_fingerprint.coordinator import FingerprintCoordinator
from custom_components.power_fingerprint.fingerprint import Fingerprint

MAINS = "sensor.mains_power"
CIRCUIT_A = "sensor.circuit_a_power"
CIRCUIT_B = "sensor.circuit_b_power"


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.coordinator


# --- seeding from the recorder ---------------------------------------------


async def test_the_window_is_seeded_from_history_so_standby_is_not_blank(
    hass: HomeAssistant, config_entry, powered, history, cycling
):
    """Otherwise every standby figure takes a day to mean anything after a
    restart, which is most of what this integration is for."""
    history[CIRCUIT_A] = cycling(1200.0)
    history[CIRCUIT_B] = cycling(300.0)

    coordinator = await _setup(hass, config_entry)

    assert coordinator.window_sizes()[CIRCUIT_A] > 10
    assert coordinator.window_hours() > 1.0
    state = hass.states.get("sensor.power_fingerprint_standby_power")
    assert state.attributes["window_ready"] is True
    assert float(state.state) >= 0.0


async def test_a_seeded_window_is_downsampled_to_the_poll_cadence(
    hass: HomeAssistant, config_entry, powered, history
):
    """A 6 s meter's 24 hours is ~14,400 rows. Appended raw, the deque keeps
    only the newest few hours and the standby percentile quietly covers an
    evening rather than a day."""
    start = dt_util.utcnow() - timedelta(hours=6)
    history[CIRCUIT_A] = [
        (start + timedelta(seconds=6 * i), 100.0) for i in range(3600)
    ]

    coordinator = await _setup(hass, config_entry)

    kept = coordinator.window_sizes()[CIRCUIT_A]
    assert kept < 3600
    # One sample per poll interval across the six hours the trace spans.
    assert abs(kept - (6 * 3600 / POLL_SECONDS)) < 20
    assert coordinator.window_hours() > 5.0


async def test_a_kilowatt_meter_is_converted_while_the_window_is_seeded(
    hass: HomeAssistant, config_entry, powered, history
):
    """History rows carry no unit, so it comes from the live state and applies
    to the whole seeded window."""
    powered(a=1.2)
    hass.states.async_set(
        CIRCUIT_A,
        1.2,
        {
            "device_class": "power",
            "state_class": "measurement",
            "unit_of_measurement": "kW",
        },
    )
    start = dt_util.utcnow() - timedelta(hours=3)
    history[CIRCUIT_A] = [(start + timedelta(seconds=60 * i), 1.2) for i in range(180)]

    coordinator = await _setup(hass, config_entry)

    assert coordinator.data["standby_total_w"] >= 1200.0


async def test_an_install_without_the_recorder_still_loads(
    hass: HomeAssistant, config_entry, powered, caplog
):
    """The recorder is an after-dependency. Without it the entry loads and the
    window fills from live polls instead - said once, in the log."""
    with patch.dict(sys.modules, {"homeassistant.components.recorder": None}):
        coordinator = await _setup(hass, config_entry)

    assert coordinator.window_sizes()[CIRCUIT_A] == 1
    assert "recorder is not available" in caplog.text


async def test_a_failed_seed_is_a_warning_not_a_silent_empty_window(
    hass: HomeAssistant, config_entry, powered, history, caplog
):
    """A silent seed failure produces standby figures that look real."""

    def _boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    with patch(
        "homeassistant.components.recorder.history.get_significant_states", _boom
    ):
        await _setup(hass, config_entry)

    assert "Could not seed the window from the recorder" in caplog.text


async def test_history_with_nothing_in_it_says_so(
    hass: HomeAssistant, config_entry, powered, history, caplog
):
    await _setup(hass, config_entry)
    assert "returned no history for any of the 2 configured circuits" in caplog.text


# --- what the poll derives -------------------------------------------------


async def test_a_sensor_that_reports_text_is_skipped_not_crashed_on(
    hass: HomeAssistant, config_entry, powered
):
    coordinator = await _setup(hass, config_entry)
    hass.states.async_set(
        CIRCUIT_B, "not a number", {"device_class": "power", "unit_of_measurement": "W"}
    )
    await coordinator.async_refresh()
    assert coordinator.data["coverage"]["circuits_w"] == 100.0


async def test_a_repair_issue_is_raised_and_cleared_with_the_unit(
    hass: HomeAssistant, config_entry, powered
):
    """The consequence of a non-power unit is a circuit silently missing from
    every total, which is invisible in the UI."""
    coordinator = await _setup(hass, config_entry)
    hass.states.async_set(
        CIRCUIT_B, 50.0, {"device_class": "power", "unit_of_measurement": "A"}
    )
    await coordinator.async_refresh()

    issues = ir.async_get(hass)
    assert issues.async_get_issue(DOMAIN, f"bad_unit_{CIRCUIT_B}") is not None

    powered()
    await coordinator.async_refresh()
    assert issues.async_get_issue(DOMAIN, f"bad_unit_{CIRCUIT_B}") is None
    assert CIRCUIT_B not in coordinator.source_profile()["rejected_non_power_units"]


async def test_a_switch_claiming_on_against_a_dead_circuit_is_a_contradiction(
    hass: HomeAssistant, config_entry, powered
):
    """The state machine says one thing and the clamp says another. The clamp
    is the one measuring reality."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            "contradiction_pairs": f"switch.heater: {CIRCUIT_B}",
        },
    )
    hass.states.async_set("switch.heater", "on")
    powered(a=100.0, b=0.0)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = config_entry.runtime_data.coordinator
    await coordinator.async_refresh()

    assert coordinator.data["contradictions"]
    state = hass.states.get("binary_sensor.power_fingerprint_state_contradiction")
    assert state.state == "on"
    assert state.attributes["detail"][0]["switch"] == "switch.heater"


async def test_a_circuit_that_never_moves_is_reported_as_silent_not_as_a_fault(
    hass: HomeAssistant, config_entry, powered
):
    """An unused circuit legitimately reads zero forever."""
    powered(mains=100.0, a=100.0, b=0.0)
    coordinator = await _setup(hass, config_entry)
    await coordinator.async_refresh()
    assert coordinator.data["silent_circuits"] == [CIRCUIT_B]


# --- blind time ------------------------------------------------------------


async def test_a_restart_gap_is_counted_as_unobserved_rather_than_as_silence(
    hass: HomeAssistant, config_entry, powered, hass_storage
):
    """Otherwise every reboot fires an absence alert on every appliance."""
    long_ago = (dt_util.utcnow() - timedelta(days=3)).isoformat()
    config_entry.add_to_hass(hass)
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": {
            "fingerprints": [
                Fingerprint(
                    label="Fridge",
                    circuit=CIRCUIT_A,
                    count=20,
                    cadence={"runs": 20, "median_gap_s": 3600.0, "p90_gap_s": 5400.0},
                ).to_dict()
            ],
            "last_seen": {f"{CIRCUIT_A}|Fridge": long_ago},
            "last_poll": long_ago,
        },
    }
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    detail = config_entry.runtime_data.coordinator.data["absence_detail"]
    assert [row["state"] for row in detail] == ["unknown"]
    assert "unobserved" in detail[0]["reason"]
    state = hass.states.get("binary_sensor.power_fingerprint_silent_appliance")
    assert state.state == "unknown"
    assert state.attributes["unjudgeable"] == ["Fridge"]


async def test_an_appliance_watched_all_along_is_reported_overdue(
    hass: HomeAssistant, config_entry, powered, hass_storage
):
    """The alert worth having: a fridge that stopped cycling crosses no
    threshold, so nothing else in the house notices."""
    now = dt_util.utcnow()
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": {
            "fingerprints": [
                Fingerprint(
                    label="Fridge",
                    circuit=CIRCUIT_A,
                    count=20,
                    cadence={"runs": 20, "median_gap_s": 3600.0, "p90_gap_s": 5400.0},
                ).to_dict()
            ],
            "last_seen": {f"{CIRCUIT_A}|Fridge": (now - timedelta(days=2)).isoformat()},
            # Polled a moment ago: the integration WAS watching.
            "last_poll": (now - timedelta(seconds=POLL_SECONDS)).isoformat(),
        },
    }
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = config_entry.runtime_data.coordinator
    assert [row["appliance"] for row in coordinator.data["absent"]] == ["Fridge"]
    state = hass.states.get("binary_sensor.power_fingerprint_silent_appliance")
    assert state.state == "on"
    assert state.attributes["overdue"] == ["Fridge"]


async def test_an_unreadable_last_poll_is_treated_as_no_gap(
    hass: HomeAssistant, config_entry, powered, hass_storage
):
    """A hand-edited or truncated .storage file must not break the poll."""
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": {"fingerprints": [], "last_poll": "not a timestamp"},
    }
    coordinator = await _setup(hass, config_entry)
    assert coordinator.data["absence_detail"] == []


async def test_a_fingerprint_never_seen_since_learning_is_not_overdue(
    hass: HomeAssistant, config_entry, powered, hass_storage
):
    """The library was built from history this coordinator did not watch, so
    the honest answer is "not yet", not "overdue"."""
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": {
            "fingerprints": [
                Fingerprint(
                    label="Fridge",
                    circuit=CIRCUIT_A,
                    count=20,
                    cadence={"runs": 20, "median_gap_s": 3600.0, "p90_gap_s": 5400.0},
                ).to_dict(),
                # No cadence learned: nothing to judge it against either.
                Fingerprint(label="Oven", circuit=CIRCUIT_B, count=6).to_dict(),
            ]
        },
    }
    coordinator = await _setup(hass, config_entry)
    assert coordinator.data["absence_detail"] == []


# --- the poll heartbeat ----------------------------------------------------


async def test_the_heartbeat_is_stamped_even_when_nothing_was_seen(
    hass: HomeAssistant, config_entry, powered
):
    """After a hard crash the un-persisted stretch is credited as blind time,
    so the heartbeat cannot wait for a sighting."""
    coordinator = await _setup(hass, config_entry)
    store = config_entry.runtime_data.store
    assert store.last_poll() is not None

    stamped = store.last_poll()
    await coordinator.async_refresh()
    # Throttled: a refresh a moment later does not re-stamp it.
    assert store.last_poll() == stamped


async def test_a_clean_unload_stamps_the_heartbeat_exactly(
    hass: HomeAssistant, config_entry, powered
):
    """So an options-change reload credits zero blind time instead of the
    whole debounce window."""
    await _setup(hass, config_entry)
    store = config_entry.runtime_data.store
    before = store.last_poll()

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert store.last_poll() != before


# --- without a store -------------------------------------------------------


async def test_a_coordinator_with_no_store_still_runs_the_label_free_checks(
    hass: HomeAssistant, config_entry, powered
):
    """The store is optional by construction - the checks that need no
    labelled fingerprints must not depend on it."""
    config_entry.add_to_hass(hass)
    coordinator = FingerprintCoordinator(hass, dict(config_entry.data), config_entry)
    await coordinator.async_refresh()

    assert coordinator.data["coverage"]["unmonitored_w"] == 5.0
    assert coordinator.data["labelled_fingerprints"] == 0
    assert coordinator.data["candidates"] == []
    assert coordinator.data["absence_detail"] == []
    # Nothing to persist to, and nothing raised trying.
    await coordinator.async_persist_seen(dt_util.utcnow())


async def test_a_last_seen_stamp_that_cannot_be_read_is_skipped(
    hass: HomeAssistant, config_entry, powered, hass_storage
):
    """A hand-edited or half-written .storage row must not break the poll for
    every other appliance."""
    hass_storage[f"{DOMAIN}.{config_entry.entry_id}"] = {
        "version": 1,
        "data": {
            "fingerprints": [
                Fingerprint(
                    label="Fridge",
                    circuit=CIRCUIT_A,
                    count=20,
                    cadence={"runs": 20, "median_gap_s": 3600.0, "p90_gap_s": 5400.0},
                ).to_dict()
            ],
            "last_seen": {f"{CIRCUIT_A}|Fridge": "half a timestamp"},
        },
    }
    coordinator = await _setup(hass, config_entry)
    assert coordinator.data["absence_detail"] == []


async def test_a_pair_is_skipped_while_either_half_is_unreadable(
    hass: HomeAssistant, config_entry, powered
):
    """A switch that has not appeared yet is not a contradiction."""
    config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        config_entry,
        data={
            **config_entry.data,
            "contradiction_pairs": f"switch.not_here: {CIRCUIT_B}",
        },
    )
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    assert config_entry.runtime_data.coordinator.data["contradictions"] == []
