"""Tests for device-to-circuit attribution."""

import importlib.util
import pathlib
import sys
from datetime import UTC, datetime, timedelta

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "attribution.py"
)
_spec = importlib.util.spec_from_file_location("pf_attribution", _PATH)
at = importlib.util.module_from_spec(_spec)
sys.modules["pf_attribution"] = at
_spec.loader.exec_module(at)

T0 = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_resample_holds_last_value_forward():
    samples = [(T0, 10.0), (T0 + timedelta(seconds=120), 50.0)]
    grid = at.resample(samples, 60, T0, T0 + timedelta(seconds=180))
    assert grid == [10.0, 10.0, 50.0, 50.0]


def test_containment_rejects_a_device_bigger_than_its_circuit():
    """A device cannot draw more than the circuit feeding it."""
    assert at.containment([500.0] * 10, [100.0] * 10) == 0.0
    assert at.containment([100.0] * 10, [500.0] * 10) == 1.0


def test_step_match_finds_a_small_load_on_a_busy_circuit():
    """The case Pearson misses: 10 W lamp, circuit swinging hundreds of watts."""
    device = [0.0, 0.0, 10.8, 10.8, 0.0, 0.0]
    circuit = [400.0, 400.0, 410.8, 410.8, 400.0, 400.0]
    assert at.step_match(device, circuit) == 1.0
    assert abs(at.pearson(device, circuit)) > 0.9  # here they agree

    noisy = [400.0, 250.0, 260.8, 900.0, 400.0, 700.0]
    # Correlation is destroyed by the other loads; steps still line up for the
    # one transition whose magnitude matches.
    assert abs(at.pearson(device, noisy)) < 0.9


def test_step_match_requires_matching_magnitude_not_just_direction():
    device = [0.0, 0.0, 10.0, 10.0]
    same_direction_wrong_size = [0.0, 0.0, 900.0, 900.0]
    assert at.step_match(device, same_direction_wrong_size) == 0.0


def test_assign_refuses_when_two_circuits_look_alike():
    """Regression: without a margin, a busy circuit absorbed six devices from
    three unrelated areas."""
    device = [0.0, 0.0, 20.0, 20.0, 0.0, 0.0]
    twin_a = [100.0, 100.0, 120.0, 120.0, 100.0, 100.0]
    twin_b = [200.0, 200.0, 220.0, 220.0, 200.0, 200.0]
    out = at.assign({"d": device}, {"a": twin_a, "b": twin_b})
    assert out["d"]["circuit"] is None
    assert out["d"]["reason"] == "ambiguous"


def test_assign_accepts_a_clear_single_winner():
    device = [0.0, 0.0, 20.0, 20.0, 0.0, 0.0]
    real = [100.0, 100.0, 120.0, 120.0, 100.0, 100.0]
    unrelated = [900.0] * 6
    out = at.assign({"d": device}, {"real": real, "unrelated": unrelated})
    assert out["d"]["circuit"] == "real"


def test_subtract_never_goes_negative():
    """Metering error between two devices can overshoot; a negative watt
    reading downstream is worse than a zero."""
    assert at.subtract([10.0, 10.0], [[30.0, 0.0]]) == [0.0, 10.0]


def test_subtract_removes_a_known_device():
    circuit = [500.0, 500.0, 500.0]
    dev = [100.0, 100.0, 100.0]
    assert at.subtract(circuit, [dev]) == [400.0, 400.0, 400.0]
