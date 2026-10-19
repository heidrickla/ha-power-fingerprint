"""Tests for lining shapes' runs up against entity state changes."""

import importlib.util
import pathlib
import random
import sys

import pytest

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "correlate.py"
)
_spec = importlib.util.spec_from_file_location("pf_correlate", _PATH)
co = importlib.util.module_from_spec(_spec)
sys.modules["pf_correlate"] = co
_spec.loader.exec_module(co)

DAY = 86400.0
WEEK = 7 * DAY


def test_transitions_group_by_the_state_entered():
    rows = [(0, "off"), (10, "on"), (20, "off"), (30, "on"), (40, "on")]
    assert co.transitions(rows) == {"on": [10, 30], "off": [20]}


def test_dropouts_are_not_events():
    rows = [(0, "off"), (10, "unavailable"), (20, "off"), (30, "unknown"), (40, "on")]
    assert co.transitions(rows) == {}


def test_timestamp_states_are_one_series():
    rows = [(0, "2026-10-01T00:00:00+00:00"), (10, "2026-10-01T00:00:10+00:00")]
    assert co.transitions(rows) == {co.FIRED: [10]}


def test_free_text_states_collapse_to_changed():
    rows = [(i, f"address {i}") for i in range(co.MAX_STATES + 2)]
    assert list(co.transitions(rows)) == [co.CHANGED]


def test_power_edges_cross_the_threshold_both_ways():
    samples = [(0, 1.0), (10, 50.0), (20, 60.0), (30, 2.0)]
    assert co.power_edges(samples, 5.0) == {"on": [10], "off": [30]}


def test_hits_counts_edges_with_an_event_inside_the_window():
    assert co.hits([100.0, 500.0], [90.0, 300.0, 520.0], 30.0) == 2
    assert co.hits([], [90.0], 30.0) == 0


def test_binomial_tail_small_cases_are_exact():
    assert co.binomial_tail(3, 0, 0.5) == 1.0
    assert co.binomial_tail(3, 4, 0.5) == 0.0
    assert co.binomial_tail(3, 3, 0.5) == pytest.approx(0.125)
    assert co.binomial_tail(4, 2, 0.5) == pytest.approx(11 / 16)


def test_binomial_tail_does_not_overflow_on_a_month_of_runs():
    assert 0.0 <= co.binomial_tail(5000, 2500, 0.01) < 1e-100


def _runs(n, seed=1):
    rng = random.Random(seed)
    starts = sorted(rng.uniform(0, WEEK - 3600) for _ in range(n))
    return starts, [s + 600 for s in starts]


def test_a_switch_at_every_start_is_clear():
    starts, ends = _runs(20)
    series = {("switch.heater", "on"): [s - 5 for s in starts]}
    found = co.score(series, starts, ends, 30.0, 0.0, WEEK)
    assert found
    best = found[0]
    assert (best.entity_id, best.edge, best.matched) == ("switch.heater", "start", 20)
    assert best.verdict == "clear"


def test_a_busy_sensor_scores_its_chance():
    """A sensor firing every two minutes all week lines up with half of any
    runs by timing alone; its chance says so and it is never clear."""
    starts, ends = _runs(20)
    busy = [float(t) for t in range(0, int(WEEK), 120)]
    assert co.chance(busy, starts, 30.0, 0.0, WEEK) > 0.35
    found = co.score(
        {("binary_sensor.hall", "on"): busy}, starts, ends, 30.0, 0.0, WEEK
    )
    assert all(m.verdict != "clear" for m in found)


def test_a_sensor_that_keeps_the_households_hours_is_not_clear():
    """Fires within a minute of each run's time of day on every day of the
    week. Hour shifts break that; the same time on other days does not."""
    starts = [d * DAY + 18 * 3600 + d * 7 for d in range(1, 6)]
    ends = [s + 600 for s in starts]
    daily = sorted(d * DAY + 18 * 3600 + 20 for d in range(7))
    c = co.chance(daily, starts, 30.0, 0.0, WEEK)
    assert c > 0.5
    found = co.score(
        {("binary_sensor.kitchen", "on"): daily}, starts, ends, 30.0, 0.0, WEEK
    )
    assert all(m.verdict != "clear" for m in found)


def test_a_single_coincidence_is_not_reported():
    starts, ends = _runs(10)
    series = {("light.porch", "on"): sorted([starts[3] + 2, 4.0])}
    assert co.score(series, starts, ends, 30.0, 0.0, WEEK) == []


def test_no_runs_scores_nothing():
    assert co.score({("light.porch", "on"): [1.0, 2.0]}, [], [], 30.0, 0.0, WEEK) == []


@pytest.mark.parametrize(
    ("domain", "device_class", "transition", "battery", "kind", "expected"),
    [
        ("light", None, "on", False, None, co.SWITCH),
        ("switch", None, "on", False, None, co.SWITCH),
        ("fan", None, "on", False, None, co.SWITCH),
        ("binary_sensor", "motion", "on", False, None, co.CONTEXT),
        ("binary_sensor", "door", "off", True, None, co.CONTEXT),
        ("binary_sensor", "running", "on", False, None, co.LOAD),
        ("sensor", "power", "on", False, None, co.METER),
        ("sensor", "enum", "charging", True, None, co.LOAD),
        ("sensor", "enum", "discharging", True, None, co.CONTEXT),
        ("device_tracker", None, "home", True, None, co.CONTEXT),
        ("device_tracker", None, "home", False, None, co.LOAD),
        ("media_player", None, "off", False, None, co.LOAD),
        ("climate", None, "hvac_action cooling", False, None, co.LOAD),
        ("event", None, co.FIRED, False, None, co.CONTEXT),
        ("light", None, "on", False, co.SENSOR, co.CONTEXT),
        ("switch", None, "on", False, co.LOAD, co.LOAD),
        ("binary_sensor", "motion", "on", False, co.SWITCH, co.SWITCH),
    ],
)
def test_role(domain, device_class, transition, battery, kind, expected):
    assert co.role(domain, device_class, transition, battery, kind) == expected


def test_a_motion_sensor_cannot_be_the_load():
    assert co.fit(co.CONTEXT, 100.0, None, None, None, None) == "not_a_load"
    assert co.fit(co.CONTEXT, 3.0, None, None, None, None) == "fits"


def test_a_meter_reading_decides_the_fit():
    assert co.fit(co.SWITCH, 100.0, 95.0, None, None, None) == "fits"
    assert co.fit(co.SWITCH, 100.0, 30.0, None, None, None) == "too_small"
    assert co.fit(co.SWITCH, 100.0, 300.0, None, None, None) == "too_large"


def test_a_rating_bounds_a_load_both_ways():
    assert co.fit(co.LOAD, 120.0, None, co.LOAD, 100.0, 400.0) == "fits"
    assert co.fit(co.LOAD, 1500.0, None, co.LOAD, 100.0, 400.0) == "too_small"
    assert co.fit(co.LOAD, 10.0, None, co.LOAD, 100.0, 400.0) == "too_large"
    assert co.fit(co.LOAD, 120.0, None, None, None, None) == "unknown"


def test_a_switch_rating_only_rules_a_switch_out():
    assert co.fit(co.SWITCH, 300.0, None, co.SWITCH, None, 600.0) == "unknown"
    assert co.fit(co.SWITCH, 2000.0, None, co.SWITCH, None, 600.0) == "too_small"


def test_a_light_over_the_signpost_stays_a_candidate():
    assert co.fit(co.SWITCH, 1200.0, None, None, None, None) == "unknown"
    assert co.draw_hint(1200.0) == "heating element or motor likelier"
    assert co.draw_hint(100.0) is None


def test_the_circuit_step_decides_when_nothing_else_does():
    assert co.fit(co.SWITCH, 600.0, None, None, None, None, 590.0) == "fits"
    assert co.fit(co.SWITCH, 600.0, None, None, None, None, -610.0) == "fits"
    assert co.fit(co.LOAD, 600.0, None, None, None, None, 40.0) == "too_small"


def test_a_rating_outranks_the_circuit_step():
    """The step at a matched entity's events is close to the shape's draw by
    construction; a specification is independent of it."""
    assert co.fit(co.LOAD, 1200.0, None, co.LOAD, 100.0, 400.0, 1190.0) == "too_small"
    assert co.fit(co.SWITCH, 300.0, None, co.SWITCH, None, 600.0, 290.0) == "fits"


def test_nothing_fits_a_shape_with_no_draw():
    assert co.fit(co.LOAD, 0.0, 10.0, None, None, None) == "unknown"


def test_step_at_reads_the_circuit_change_across_events():
    times = [float(t) for t in range(0, 600, 6)]
    watts = [10.0 if t < 300 else 610.0 for t in times]
    assert co.step_at(times, watts, [300.0]) == pytest.approx(600.0)
    assert co.step_at(times, watts, [700.0]) == pytest.approx(0.0)
    assert co.step_at([], [], [300.0]) is None


def test_step_at_holds_the_last_reading_through_a_quiet_minute():
    """The recorder keeps only changes: a flat stretch has no rows in it."""
    times, watts = [0.0, 290.0], [10.0, 410.0]
    assert co.step_at(times, watts, [280.0]) == pytest.approx(400.0)


def test_match_to_dict_rounds_for_the_response():
    m = co.Match("light.a", "on", "start", 3, 4, 10, 0.012345, 0.000123456)
    d = m.to_dict()
    assert d["chance"] == 0.0123
    assert d["expected_false"] == 0.000123
    assert d["verdict"] == "clear"
