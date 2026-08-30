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


# --- devices that never switched in the window -----------------------------
#
# From the development install: a rack PDU drifted between 211 and 227 W for
# three days without one step above threshold, and was "contained" by its
# circuit in all 43,201 samples. That reads as a confident placement and means
# nothing.
#
# These are NOT constant loads. Network gear draws very differently when it
# starts; it simply never gets switched, because switching it takes the network
# down. A window containing one real power cut places them instantly.


def _rack(n: int = 400) -> list[float]:
    """A rack PDU: slow drift inside a narrow band, no step, like the real one."""
    return [211.0 + (i * 16 / n) for i in range(n)]


def _motion_light(n: int = 400) -> list[float]:
    """On for 3% of the window. Its p95 and p5 are both "off"."""
    return [11.0 if 100 <= i < 112 else 0.0 for i in range(n)]


def test_a_device_that_never_switched_is_not_traceable():
    rack = _rack()
    assert at.transitions(rack) == 0
    assert not at.traceable(rack)


def test_a_rarely_used_light_IS_traceable():
    """The regression that killed the percentile version of this test.

    A light on for 3% of the window has an identical 95th and 5th percentile,
    so a spread-based check called it untraceable while it was switching
    several times a day. Steps see it immediately.
    """
    light = _motion_light()
    assert at.transitions(light) == 2  # on, then off
    assert at.traceable(light)


def test_slow_drift_is_not_a_transition():
    """The other half: drift moves a trace without ever stepping."""
    assert at.transitions([211.0 + i * 0.04 for i in range(400)]) == 0


def test_assign_says_no_transition_rather_than_no_candidate():
    """The whole point: the two answers must not look the same.

    Containment would happily place this device - the circuit is above it at
    every sample - which is exactly the false confidence being refused.
    """
    circuit = [900.0 + (i * 37 % 401) for i in range(400)]
    result = at.assign({"sensor.rack": _rack()}, {"sensor.circuit": circuit})
    assert result["sensor.rack"]["circuit"] is None
    assert result["sensor.rack"]["reason"] == "no transition in window"


def test_an_unswitched_device_cannot_consume_a_real_device_s_circuit():
    """Set aside BEFORE the subtraction loop, not after.

    A device that squeaked through on containment would have its trace
    subtracted from the circuit, stripping the steps the real device needs.
    """
    lamp = [0.0 if (i // 20) % 2 else 60.0 for i in range(400)]
    circuit = [800.0 + (0.0 if (i // 20) % 2 else 60.0) for i in range(400)]
    result = at.assign(
        {"sensor.rack": _rack(), "sensor.lamp": lamp}, {"sensor.circuit": circuit}
    )
    assert result["sensor.rack"]["reason"] == "no transition in window"
    assert result["sensor.lamp"]["circuit"] == "sensor.circuit"
