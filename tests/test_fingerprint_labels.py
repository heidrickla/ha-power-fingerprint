"""Tests for label carrying across a re-learn."""

import importlib.util
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "fingerprint.py"
)
_spec = importlib.util.spec_from_file_location("pf_fingerprint", _PATH)
fp = importlib.util.module_from_spec(_spec)
sys.modules["pf_fingerprint"] = fp
_spec.loader.exec_module(fp)


def make(label, circuit="sensor.c1", count=1):
    return fp.Fingerprint(label=label, circuit=circuit, count=count)


def test_unnamed_is_not_an_identification():
    assert not fp.is_named(make("unnamed_0"))
    assert fp.is_named(make("Dryer"))


def test_relearning_keeps_human_labels():
    old = [make("Dryer"), make("Washer")]
    new = [make("unnamed_0"), make("unnamed_1")]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["Dryer", "Washer"]


def test_relearning_does_not_invent_labels_for_extra_clusters():
    """A re-run finding MORE shapes must leave the new ones unnamed."""
    old = [make("Dryer")]
    new = [make("unnamed_0"), make("unnamed_1"), make("unnamed_2")]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["Dryer", "unnamed_1", "unnamed_2"]


def test_unnamed_old_labels_are_not_carried():
    """Carrying 'unnamed_0' forward would be noise, not information."""
    old = [make("unnamed_0"), make("Washer")]
    new = [make("unnamed_0"), make("unnamed_1")]
    out = fp.carry_labels(old, new)
    assert [f.label for f in out] == ["unnamed_0", "Washer"]


def test_match_refuses_rather_than_guessing():
    """An unmatched event means something new was plugged in - it must not be
    forced into the nearest bucket."""
    library = [
        fp.Fingerprint(
            label="Dryer",
            circuit="sensor.c1",
            count=5,
            centroid={
                "peak_w": 800.0,
                "floor_w": 380.0,
                "mean_w": 400.0,
                "plateaus": 3,
                "duty_above_half_peak": 0.9,
            },
        )
    ]
    nothing_like_it = {
        "peak_w": 12.0,
        "floor_w": 1.0,
        "mean_w": 8.0,
        "plateaus": 1,
        "duty_above_half_peak": 0.1,
    }
    matched, dist = fp.match(nothing_like_it, library)
    assert matched is None
    assert dist > 1.0
