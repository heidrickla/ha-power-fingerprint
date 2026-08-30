"""Virtual circuits: large appliances pulled out of a whole-house trace.

Loaded by path like the other pure modules - `virtual` imports nothing from
Home Assistant so it can be tested without it.
"""

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "virtual.py"
)
_spec = importlib.util.spec_from_file_location("pf_virtual", _PATH)
v = importlib.util.module_from_spec(_spec)
sys.modules["pf_virtual"] = v
_spec.loader.exec_module(v)

T0 = datetime(2026, 8, 30, tzinfo=UTC)


def trace(values, step_s=6):
    return [
        (T0 + timedelta(seconds=step_s * i), float(x)) for i, x in enumerate(values)
    ]


def test_a_single_appliance_is_recovered_from_a_busy_house():
    """900 W of background, then a 3 kW load for a while, then off again."""
    samples = trace([900] * 20 + [3900] * 50 + [900] * 20)
    events, unpaired = v.pair_steps(samples, floor_w=300.0)
    assert len(events) == 1
    assert round(events[0].magnitude_w) == 3000
    assert round(events[0].baseline_w) == 900
    assert events[0].duration_s == 50 * 6
    assert unpaired == []


def test_nested_appliances_pair_with_the_right_partner():
    """⛔ The regression that oldest-first matching gets wrong.

    Oven on, kettle on and off inside it, oven off. Pairing the oven's start
    with the kettle's stop invents a short 3 kW run and leaves the oven
    unaccounted.
    """
    samples = trace([500] * 10 + [3500] * 10 + [5500] * 10 + [3500] * 10 + [500] * 10)
    events, unpaired = v.pair_steps(samples, floor_w=300.0)
    assert len(events) == 2
    sizes = sorted(round(e.magnitude_w) for e in events)
    assert sizes == [2000, 3000]
    oven = next(e for e in events if round(e.magnitude_w) == 3000)
    kettle = next(e for e in events if round(e.magnitude_w) == 2000)
    # The kettle must sit INSIDE the oven's run, not straddle its end.
    assert oven.start < kettle.start and kettle.end < oven.end
    assert unpaired == []


def test_a_load_that_never_switched_off_is_reported_unpaired_not_dropped():
    """ "I could not tell" must not silently become "it did not happen"."""
    samples = trace([800] * 10 + [4800] * 40)
    events, unpaired = v.pair_steps(samples, floor_w=300.0)
    assert events == []
    assert len(unpaired) == 1
    assert unpaired[0][1] > 0  # an up-step, still open


def test_mismatched_magnitudes_do_not_pair():
    """A 3 kW start and a 700 W stop are two different appliances."""
    samples = trace([500] * 10 + [3500] * 10 + [2800] * 10)
    events, unpaired = v.pair_steps(samples, floor_w=300.0)
    assert events == []
    assert len(unpaired) == 2


def test_small_loads_are_below_the_floor_and_ignored():
    """A 60 W lamp on a house drawing a kilowatt is not a virtual circuit."""
    samples = trace([1000] * 10 + [1060] * 10 + [1000] * 10)
    events, unpaired = v.pair_steps(samples, floor_w=300.0)
    assert events == []
    assert unpaired == []


def test_noise_floor_is_measured_from_the_trace():
    quiet = trace([1000 + (i % 3) for i in range(200)])
    busy = trace([1000 + (i * 137 % 900) for i in range(200)])
    assert v.noise_floor(quiet) < v.noise_floor(busy)


def test_resolvable_floor_never_goes_below_the_measured_limit():
    """Even on a very quiet meter, do not claim to resolve a 50 W load.

    Below 300 W the measured recovery rate on a real house fell to 70% and kept
    falling, so a quiet trace must not talk the floor down past it.
    """
    quiet = trace([1000] * 200)
    assert v.noise_floor(quiet) == 0.0
    assert v.resolvable_floor(quiet) == 300.0


def test_resolvable_floor_rises_on_a_noisy_meter():
    busy = trace([1000 + (i * 311 % 4000) for i in range(400)])
    assert v.resolvable_floor(busy) > 300.0


def test_features_use_the_baseline_not_a_fabricated_minimum():
    """On an aggregate the appliance's own floor is not observable."""
    samples = trace([900] * 10 + [3900] * 20 + [900] * 10)
    events, _ = v.pair_steps(samples, floor_w=300.0)
    f = events[0].as_features()
    assert f["floor_w"] == 900.0
    assert f["peak_w"] == 3900.0


def test_a_ramping_appliance_is_one_step_not_five():
    """⛔ The regression that killed adjacent-sample stepping.

    A 2000 W compressor arriving over half a minute is five 400 W deltas. The
    old detector saw none of them (each below the floor) and the house looked
    quiet; clustering the fragments produced four "virtual circuits" that were
    two air conditioners chopped up.
    """
    ramp = [900, 1300, 1700, 2100, 2500, 2900]
    samples = trace([900] * 12 + ramp + [2900] * 12 + list(reversed(ramp)) + [900] * 12)
    found = v.steps(samples, floor_w=1000.0)
    ups = [d for _, d in found if d > 0]
    downs = [d for _, d in found if d < 0]
    assert len(ups) == 1, f"expected one up-shift, got {found}"
    assert len(downs) == 1
    assert ups[0] > 1500


def test_a_ramping_appliance_pairs_into_one_run():
    ramp = [900, 1300, 1700, 2100, 2500, 2900]
    samples = trace([900] * 12 + ramp + [2900] * 30 + list(reversed(ramp)) + [900] * 12)
    events, unpaired = v.pair_steps(samples, floor_w=1000.0)
    assert len(events) == 1
    assert events[0].magnitude_w > 1500
    assert unpaired == []
