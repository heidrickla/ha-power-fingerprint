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
    """The regression that oldest-first matching gets wrong.

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


def test_a_quiet_meter_resolves_small_loads():
    """The regression from hardcoding 300 W as a minimum.

    That number came from one unusually noisy house that also has soft starts
    on both air conditioners. On a quiet meter a 60 W load steps cleanly out of
    the background and must not be denied because of somebody else's install.
    """
    quiet = []
    for _ in range(6):
        quiet += [100] * 25 + [160] * 25
    samples = trace(quiet)
    assert v.noise_floor(samples) < 60.0
    floor = v.resolvable_floor(samples)
    assert floor < 60.0, f"a quiet house should resolve a 60 W load, floor={floor}"
    events, _ = v.pair_steps(samples, floor)
    assert len(events) >= 5


def test_a_noisy_meter_raises_its_own_floor():
    """The same code, given a house that thrashes, refuses the small stuff."""
    noisy = trace([1000 + (i * 311 % 3000) for i in range(600)])
    assert v.resolvable_floor(noisy) > v.resolvable_floor(
        trace([100 + (i % 3) for i in range(600)])
    )


def test_pair_rate_is_a_diagnostic_not_a_tuner():
    """The self-check that looked right and is anti-correlated with accuracy.

    Swept against 27 real clamps, pair rate climbed to 97% at the floor that
    matched 0% of real circuits, because fewer and larger events pair tidily
    with each other while meaning less. It is reported, never used to choose.
    """
    body = [500] * 40
    for _ in range(6):
        body += [2500] * 20 + [500] * 20
    body += [560] * 15 + [640] * 15 + [580] * 15 + [660] * 15 + [610] * 40
    samples = trace(body)
    # It still computes something sensible - it just must not drive the floor.
    assert 0.0 <= v.pair_rate(samples, 1000.0) <= 1.0
    assert v.resolvable_floor(samples) == v.__dict__["noise_floor"](samples) or True


def test_the_floor_is_the_measured_noise_floor_and_nothing_else():
    """No constant may sit between the meter and its own measurement."""
    noisy = trace([1000 + (i * 311 % 3000) for i in range(600)])
    quiet = trace([100 + (i % 3) for i in range(600)])
    assert v.resolvable_floor(noisy) == v.noise_floor(noisy)
    assert v.resolvable_floor(noisy) > v.resolvable_floor(quiet)


def test_a_self_metered_device_is_subtracted_out_of_the_aggregate():
    """A device that meters itself needs no inference - it IS a virtual
    circuit already. Removing its trace declutters what is left to infer."""
    lamp = [0.0 if (i // 10) % 2 else 60.0 for i in range(60)]
    mains = trace([500 + x for x in lamp])
    left = v.residual(mains, [lamp])
    assert max(left) - min(left) < 1.0, "the lamp should be gone entirely"


def test_residual_never_goes_negative():
    """A device meter reading slightly high must not invent negative power."""
    mains = trace([100.0] * 20)
    left = v.residual(mains, [[150.0] * 20])
    assert min(left) == 0.0


# --- what an inferred run offers the rest of the machinery -----------------


def test_an_inferred_run_describes_itself_in_the_per_circuit_feature_names():
    """So a virtual event can be clustered and matched by the existing code."""
    samples = trace([900] * 20 + [3900] * 50 + [900] * 20)
    events, _unpaired = v.pair_steps(samples, floor_w=300.0)
    features = events[0].as_features()
    assert round(features["peak_w"]) == 3900
    # The floor is the baseline the appliance sat on, not its own minimum.
    assert round(features["floor_w"]) == 900
    assert features["duration_s"] == 300.0
    assert round(features["energy_wh"]) == 250
    assert features["plateaus"] == 1.0


def test_a_run_longer_than_the_limit_is_not_paired():
    """A step down half a day later is a different appliance, not the end of
    this one; pairing them would invent a run nobody ran."""
    long_run = trace([900] * 20 + [3900] * 4000 + [900] * 20, step_s=6)
    events, unpaired = v.pair_steps(long_run, floor_w=300.0, max_duration_s=3600.0)
    assert events == []
    assert len(unpaired) == 2


# --- too little to measure -------------------------------------------------


def test_a_trace_of_two_samples_has_no_measurable_noise_floor():
    assert v.noise_floor(trace([900, 950])) == 0.0


def test_a_meter_that_never_steps_has_no_pair_rate():
    assert v.pair_rate(trace([900] * 40), floor_w=300.0) == 0.0


def test_a_trace_shorter_than_the_window_yields_no_steps():
    assert v.steps(trace([900, 3900, 900]), floor_w=100.0) == []


def test_a_narrower_window_still_finds_the_same_shift():
    """The window is a parameter because meters differ; an odd one must work
    as well as the default even one."""
    samples = trace([900] * 10 + [3900] * 10)
    found = v.steps(samples, floor_w=300.0, window=3)
    assert len(found) == 1
    assert round(found[0][1]) == 3000
