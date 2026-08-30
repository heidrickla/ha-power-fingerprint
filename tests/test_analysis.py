"""Tests for the pure-analysis layer.

`analysis.py` deliberately imports nothing from Home Assistant, so it is loaded
by path here rather than as part of the integration package - the package
`__init__` does pull HA in.
"""

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "analysis.py"
)
_spec = importlib.util.spec_from_file_location("pf_analysis", _PATH)
pf = importlib.util.module_from_spec(_spec)
sys.modules["pf_analysis"] = pf  # dataclass needs the module resolvable
_spec.loader.exec_module(pf)

T0 = datetime(2026, 8, 28, 2, 0, tzinfo=UTC)


def trace(values, step_s=12):
    """Build a sample series at a realistic Emporia cadence."""
    return [(T0 + timedelta(seconds=i * step_s), v) for i, v in enumerate(values)]


# --------------------------------------------------------------- thresholds


def test_floor_ignores_a_single_dropout():
    """One zero reading during a meter blip must not zero the standby figure."""
    samples = trace([100.0] * 99 + [0.0])
    assert pf.circuit_floor(samples) == 100.0


def test_threshold_works_on_a_high_baseline_circuit():
    """Regression: `idle * 3` scored zero runs on a circuit sitting at 663 W,
    because it landed above that circuit's own p99."""
    samples = trace([663.0] * 90 + [915.0] * 10)
    thr = pf.on_threshold(samples)
    assert 663.0 < thr < 915.0, thr
    assert len(pf.segment(samples, min_duration_s=0)) == 1


# ---------------------------------------------------------------- segmenting


def test_short_load_is_not_discarded():
    """Regression: a 20-second garbage disposal was dropped by a 2-minute floor."""
    samples = trace([1.0] * 20 + [423.0, 423.0] + [1.0] * 20)
    assert pf.segment(samples, min_duration_s=10.0)


def test_gap_tolerance_keeps_one_wash_as_one_event():
    """A washer dipping to near zero during soak is still one wash."""
    cycle = [400.0] * 5 + [2.0] * 3 + [400.0] * 5 + [2.0] * 3 + [400.0] * 5
    samples = trace([1.0] * 10 + cycle + [1.0] * 10)
    assert len(pf.segment(samples, gap_tolerance_s=60.0)) == 1


def test_gap_tolerance_zero_splits_them():
    cycle = [400.0] * 5 + [2.0] * 3 + [400.0] * 5
    samples = trace([1.0] * 10 + cycle + [1.0] * 10)
    assert len(pf.segment(samples, gap_tolerance_s=0.0, min_duration_s=0)) == 2


# ------------------------------------------------- the washer/dryer separator


def test_floor_while_running_separates_dryer_from_washer():
    """The whole reason `floor_w` exists.

    Both appliances share one circuit and have near-identical peaks. Only the
    minimum while running tells them apart.
    """
    dryer_trace = trace([1.0] * 5 + [380.0, 400.0, 830.0, 385.0, 375.0] + [1.0] * 5)
    washer_trace = trace([1.0] * 5 + [820.0, 60.0, 90.0, 800.0, 70.0] + [1.0] * 5)
    dryer = pf.segment(dryer_trace, min_duration_s=0)[0]
    washer = pf.segment(washer_trace, min_duration_s=0)[0]

    assert abs(dryer.peak_w - washer.peak_w) < 50  # peaks do NOT separate them
    assert dryer.floor_w > 300  # floors do
    assert washer.floor_w < 100


# ----------------------------------------------------------------- coverage


def test_coverage_matches_the_measured_install():
    """27 circuits summing to 6527 W against a 6556 W mains reading."""
    cov = pf.coverage(6556.0, {f"c{i}": v for i, v in enumerate([6527.0])})
    assert cov["unmonitored_w"] == 29.0
    assert cov["coverage_pct"] == 99.6


def test_coverage_handles_a_zero_mains_reading():
    assert pf.coverage(0.0, {"a": 0.0})["coverage_pct"] == 0.0


# ------------------------------------------------------------ contradictions


def test_contradiction_flags_a_switch_with_no_load():
    out = pf.contradictions([("switch.lamp", "sensor.c1", True, 0.2)])
    assert len(out) == 1 and out[0]["switch"] == "switch.lamp"


def test_no_contradiction_when_the_switch_is_off():
    assert pf.contradictions([("switch.lamp", "sensor.c1", False, 0.0)]) == []


def test_no_contradiction_when_current_is_flowing():
    assert pf.contradictions([("switch.lamp", "sensor.c1", True, 60.0)]) == []


# ------------------------------------------------------------------- parsing


def test_parse_pairs_skips_junk():
    raw = "switch.a: sensor.b\n\n# a comment\nnonsense\nswitch.c:sensor.d\n"
    assert pf.parse_pairs(raw) == [("switch.a", "sensor.b"), ("switch.c", "sensor.d")]


def test_parse_pairs_tolerates_empty():
    assert pf.parse_pairs("") == []
    assert pf.parse_pairs(None) == []


# -------------------------------------------------------------------- ranking


def test_standby_ranking_is_ordered_and_costed():
    rows = pf.standby_ranking({"a": 642.2, "b": 30.0}, 0.13)
    assert [r["circuit"] for r in rows] == ["a", "b"]
    assert rows[0]["annual_cost"] == round(642.2 * 8.766 * 0.13, 2)


# --- units -----------------------------------------------------------------
#
# device_class: power says nothing about the unit, and a kW meter fed straight
# into a watt-denominated threshold produces zero events forever rather than an
# error. These tests exist because that is the same silent-empty failure the
# percentile threshold produced, and it is not visible from the outside.


def test_to_watts_passes_watts_through():
    assert pf.to_watts(1234.0, "W") == 1234.0


def test_to_watts_scales_kilowatts():
    assert pf.to_watts(4.2, "kW") == 4200.0


def test_to_watts_scales_milliwatts_and_megawatts():
    assert pf.to_watts(2500.0, "mW") == 2.5
    assert pf.to_watts(0.5, "MW") == 500_000.0


def test_to_watts_converts_thermal_btu():
    # UnitOfPower includes BTU/h; a heat pump integration really does use it.
    assert round(pf.to_watts(10_000.0, "BTU/h")) == 2931


def test_to_watts_treats_a_missing_unit_as_watts():
    # Template sensors routinely omit the unit. Rejecting them would break
    # working installs, and W-vs-kW is always declared where it matters.
    assert pf.to_watts(60.0, None) == 60.0
    assert pf.to_watts(60.0, "") == 60.0


def test_to_watts_refuses_a_unit_that_is_not_power():
    assert pf.to_watts(0.9, "%") is None
    assert pf.to_watts(12.0, "A") is None


def test_to_watts_tolerates_a_missing_value():
    assert pf.to_watts(None, "kW") is None


def test_kilowatt_circuit_is_detectable_only_after_conversion():
    """The actual regression: a real load on a kW meter, end to end.

    A 4 kW dryer against a 0.05 kW standby arrives as 4.0 and 0.05. Raw, the
    whole circuit sits below the 15 W margin, so on_threshold reports a
    threshold above every sample and the circuit shows zero runs. Converted, it
    is an ordinary load.
    """
    base = datetime(2026, 8, 30, tzinfo=UTC)
    raw = [50.0] * 40 + [4000.0] * 40  # what the meter means, in watts
    kilowatts = [w / 1000.0 for w in raw]

    unconverted = [
        (base + timedelta(seconds=12 * i), kw) for i, kw in enumerate(kilowatts)
    ]
    converted = [
        (base + timedelta(seconds=12 * i), pf.to_watts(kw, "kW"))
        for i, kw in enumerate(kilowatts)
    ]

    assert max(w for _, w in unconverted) < pf.on_threshold(unconverted)
    assert pf.segment(unconverted) == []

    events = pf.segment(converted)
    assert len(events) == 1
    assert round(events[0].peak_w) == 4000


# --- cadence ---------------------------------------------------------------


def _at(base, seconds):
    return [(base + timedelta(seconds=s), 100.0) for s in seconds]


def test_sample_interval_measures_a_regular_meter():
    base = datetime(2026, 8, 30, tzinfo=UTC)
    assert pf.sample_interval(_at(base, range(0, 600, 12))) == 12.0


def test_sample_interval_ignores_dropouts():
    """The median, not the mean - a restart leaves an hour-long hole, and a
    mean would put the grid far past anything the meter actually does."""
    base = datetime(2026, 8, 30, tzinfo=UTC)
    seconds = [*range(0, 240, 12), 3840, 3852, 3864]
    measured = pf.sample_interval(_at(base, seconds))
    assert measured == 12.0


def test_sample_interval_handles_a_fast_meter():
    base = datetime(2026, 8, 30, tzinfo=UTC)
    assert pf.sample_interval(_at(base, range(0, 100))) == 1.0


def test_sample_interval_needs_something_to_measure():
    base = datetime(2026, 8, 30, tzinfo=UTC)
    assert pf.sample_interval([]) is None
    assert pf.sample_interval(_at(base, [0, 12])) is None


def test_sample_interval_ignores_duplicate_timestamps():
    """Two readings stamped identically are not a zero-second cadence."""
    base = datetime(2026, 8, 30, tzinfo=UTC)
    samples = _at(base, [0, 0, 12, 24, 24, 36])
    assert pf.sample_interval(samples) == 12.0


# --- absence detection -----------------------------------------------------
#
# Every other check here alerts on too much. The expensive failures are the
# quiet ones, and the trap is that "I did not see it run" and "it did not run"
# look identical from the outside.


def _every(hours: float, n: int, jitter_h: float = 0.0) -> list[datetime]:
    base = datetime(2026, 8, 1, tzinfo=UTC)
    return [
        base + timedelta(hours=hours * i + (jitter_h if i % 2 else 0.0))
        for i in range(n)
    ]


def test_cadence_learns_a_rhythm_from_the_runs_themselves():
    c = pf.cadence(_every(6.0, 12))
    assert c is not None
    assert c.runs == 12
    assert round(c.median_gap_s / 3600) == 6


def test_cadence_refuses_to_characterise_too_few_runs():
    """Four gaps cannot tell "runs weekly" from "ran four times and stopped"."""
    assert pf.cadence(_every(6.0, 4)) is None
    assert pf.cadence([]) is None


def test_absence_is_ok_inside_the_expected_window():
    c = pf.cadence(_every(6.0, 12))
    state, _why = pf.absence(c, silent_s=5 * 3600)
    assert state == "ok"


def test_absence_flags_a_genuinely_silent_appliance():
    c = pf.cadence(_every(6.0, 12))
    state, why = pf.absence(c, silent_s=40 * 3600)
    assert state == "overdue"
    assert "limit" in why


def test_blind_time_is_not_counted_as_silence():
    """⛔ The failure that matters: the recorder was down, not the fridge.

    Thirty hours of silence, twenty of which nobody was watching, is ten hours
    of observed silence - inside the limit. Claiming otherwise would be
    asserting an observation that was never made.
    """
    c = pf.cadence(_every(6.0, 12))
    assert pf.absence(c, silent_s=30 * 3600, blind_s=0.0)[0] == "overdue"
    assert pf.absence(c, silent_s=30 * 3600, blind_s=20 * 3600)[0] == "unknown"


def test_a_mostly_blind_window_answers_unknown_not_ok():
    """`unknown` is the honest state, and is NOT the same as `ok`.

    Returning `ok` would say the appliance is fine; returning `unknown` says
    nobody looked. Only one of those is true.
    """
    c = pf.cadence(_every(6.0, 12))
    state, why = pf.absence(c, silent_s=10 * 3600, blind_s=9 * 3600)
    assert state == "unknown"
    assert "unobserved" in why


def test_an_uncharacterised_appliance_is_unknown_not_overdue():
    state, why = pf.absence(None, silent_s=10_000 * 3600)
    assert state == "unknown"
    assert "rhythm" in why


def test_patience_scales_the_limit():
    c = pf.cadence(_every(6.0, 12))  # p90 gap is 6 h
    silent = 9 * 3600
    assert pf.absence(c, silent, patience=2.0)[0] == "ok"  # limit 12 h
    assert pf.absence(c, silent, patience=1.0)[0] == "overdue"  # limit 6 h


# --- evidence and its control ----------------------------------------------
#
# Every one of these thresholds exists because a real measurement fooled a
# human first. The point is that the code now does the checking.


def test_a_perfect_score_that_chance_also_achieves_is_chance():
    """The four "virtual circuits" that each matched both air conditioners."""
    ev = pf.control(lambda off: 0.95 if off == 0 else 0.93)
    assert ev.verdict == "chance"
    assert round(ev.lift, 2) == 0.02


def test_the_refrigerator_case_reads_as_weak_not_clear():
    """91% real, 78% at +3 h, 40% at +19 h. The lowest chance is what counts."""
    scores = {0.0: 0.91, 3.0: 0.78, 7.0: 0.64, 13.0: 0.45, 19.0: 0.40}
    ev = pf.control(lambda off: scores[off])
    assert ev.chance == 0.40
    assert round(ev.lift, 2) == 0.51
    assert ev.verdict == "clear"


def test_autocorrelation_alone_is_not_signal():
    """A shift of a few hours still overlaps the house's daily rhythm, so a
    single short offset would understate chance badly."""
    scores = {0.0: 0.80, 3.0: 0.78, 7.0: 0.74, 13.0: 0.72, 19.0: 0.70}
    ev = pf.control(lambda off: scores[off])
    assert ev.verdict == "chance"


def test_a_low_score_is_refused_however_big_the_lift():
    """Beating chance is necessary, not sufficient - 30% is still 30%."""
    ev = pf.control(lambda off: 0.30 if off == 0 else 0.01)
    assert ev.verdict == "no"


def test_a_clean_result_survives_its_control():
    ev = pf.control(lambda off: 0.95 if off == 0 else 0.10)
    assert ev.verdict == "clear"
    assert ev.to_dict()["lift"] == 0.85


# --- confidence profiles ----------------------------------------------------


def _profiles():
    import importlib.util
    import pathlib

    path = (
        pathlib.Path(__file__).resolve().parents[1]
        / "custom_components"
        / "power_fingerprint"
        / "const.py"
    )
    spec = importlib.util.spec_from_file_location("pf_const", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_profiles_are_ordered_not_just_different():
    """⛔ A dial only means something if turning it moves every bar the same way.

    Three profiles with thresholds that disagree about which is stricter would
    be worse than one profile, because the user's mental model would be wrong.
    """
    c = _profiles()
    strict, mid, loose = (
        c.CONFIDENCE_PROFILES["cautious"],
        c.CONFIDENCE_PROFILES["balanced"],
        c.CONFIDENCE_PROFILES["eager"],
    )
    for key in ("min_step_match", "min_margin", "min_correlation", "min_lift"):
        assert strict[key] > mid[key] > loose[key], key
    assert strict["probes"] > mid["probes"] > loose["probes"]
    # A tighter cluster threshold splits shapes more readily, so it inverts.
    assert (
        strict["cluster_threshold"]
        < mid["cluster_threshold"]
        < loose["cluster_threshold"]
    )


def test_an_unknown_profile_falls_back_to_balanced_not_to_eager():
    """A typo must never quietly loosen the thresholds."""
    c = _profiles()
    assert c.profile("nonsense") == c.CONFIDENCE_PROFILES["balanced"]
    assert c.profile(None) == c.CONFIDENCE_PROFILES["balanced"]
