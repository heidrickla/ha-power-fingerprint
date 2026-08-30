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
