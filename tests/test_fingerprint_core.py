"""Clustering, centroids, matching and the on-disk library.

`fingerprint.py` imports nothing from Home Assistant, so it is loaded by path
like the other pure modules. The label-carrying half is in
test_fingerprint_labels.py; this file covers the parts the learn action and the
live matcher actually run.
"""

import importlib.util
import json
import pathlib
import sys

_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components"
    / "power_fingerprint"
    / "fingerprint.py"
)
_spec = importlib.util.spec_from_file_location("pf_fingerprint_core", _PATH)
fp = importlib.util.module_from_spec(_spec)
sys.modules["pf_fingerprint_core"] = fp
_spec.loader.exec_module(fp)


def run(peak, floor, mean=None, plateaus=2, duty=0.6, duration=600.0):
    """One event's feature vector, the shape `Event.as_features()` produces."""
    return {
        "peak_w": peak,
        "floor_w": floor,
        "mean_w": mean if mean is not None else (peak + floor) / 2,
        "plateaus": plateaus,
        "duty_above_half_peak": duty,
        "duration_s": duration,
        "energy_wh": peak * duration / 3600.0,
    }


# --- clustering ------------------------------------------------------------


def test_nothing_to_cluster_is_no_clusters():
    assert fp.cluster([]) == []


def test_one_appliance_run_repeatedly_is_one_cluster():
    """The same machine under varying load must not split.

    A dryer's peak moves with the load; if that split it into clusters, every
    circuit would report a dozen shapes and none of them nameable.
    """
    rows = [run(2400, 350), run(2500, 340), run(2380, 360), run(2450, 355)]
    labels = fp.cluster(fp.normalize(rows))
    assert len(set(labels)) == 1


def test_a_washer_and_a_dryer_on_one_circuit_are_two_clusters():
    """Near-identical peaks, different floors - design note 3."""
    dryer = [run(2400, 350), run(2450, 360), run(2380, 340)]
    washer = [run(2300, 5), run(2350, 8), run(2280, 4)]
    labels = fp.cluster(fp.normalize(dryer + washer))
    assert len(set(labels)) == 2
    assert len(set(labels[:3])) == 1
    assert len(set(labels[3:])) == 1


def test_the_largest_cluster_is_cluster_zero():
    """Ordered largest first, which is what `carry_labels` relies on."""
    rows = [run(2400, 350), run(2450, 355), run(2420, 345), run(120, 2)]
    labels = fp.cluster(fp.normalize(rows))
    assert labels[-1] != 0
    assert labels.count(0) == 3


def test_a_loose_threshold_merges_what_a_tight_one_separates():
    rows = [run(2400, 350), run(2300, 5)]
    assert len(set(fp.cluster(fp.normalize(rows), threshold=0.1))) == 2
    assert len(set(fp.cluster(fp.normalize(rows), threshold=9.0))) == 1


# --- centroids and descriptions --------------------------------------------


def test_summarize_reports_one_fingerprint_per_cluster_with_its_count():
    rows = [run(2400, 350), run(2500, 340), run(120, 2)]
    prints = fp.summarize("sensor.circuit_21_power", rows, [0, 0, 1])
    assert [p.label for p in prints] == ["unnamed_0", "unnamed_1"]
    assert [p.count for p in prints] == [2, 1]
    assert all(p.circuit == "sensor.circuit_21_power" for p in prints)
    # The centroid is the median of its members, and the spread half the range.
    assert prints[0].centroid["peak_w"] == 2500.0
    assert prints[0].spread["peak_w"] == 50.0


def test_summarize_attaches_each_clusters_own_rhythm():
    """Cadence is learned per cluster and is what absence detection judges."""
    rows = [run(2400, 350), run(120, 2)]
    rhythms = {0: {"runs": 6, "median_gap_s": 3600.0, "p90_gap_s": 5400.0}}
    prints = fp.summarize("sensor.c", rows, [0, 1], rhythms)
    assert prints[0].cadence == rhythms[0]
    # No rhythm learned is None, not a fabricated zero.
    assert prints[1].cadence is None


def test_describe_reads_as_english_a_person_can_recognise():
    shape = fp.summarize("sensor.c", [run(2400, 350, duration=1800.0)], [0])[0]
    text = shape.describe()
    assert "30 min" in text
    assert "peaks 2400 W" in text
    assert "holds a 350 W floor" in text


def test_describe_reports_a_short_run_in_seconds():
    shape = fp.summarize("sensor.c", [run(1200, 30, duration=45.0)], [0])[0]
    assert "45 s" in shape.describe()


def test_describe_says_whether_a_load_is_sustained_or_spiky():
    sustained = fp.summarize("sensor.c", [run(2400, 350, duty=0.95)], [0])[0]
    spiky = fp.summarize("sensor.c", [run(2400, 350, duty=0.1)], [0])[0]
    assert "sustained, not spiky" in sustained.describe()
    assert "brief spikes" in spiky.describe()


# --- matching --------------------------------------------------------------


def test_an_empty_library_matches_nothing():
    best, distance = fp.match(run(2400, 350), [])
    assert best is None
    assert distance == float("inf")


def test_the_nearest_centroid_wins():
    dryer = fp.Fingerprint(
        label="Dryer", circuit="sensor.c", count=9, centroid=run(2400, 350)
    )
    washer = fp.Fingerprint(
        label="Washer", circuit="sensor.c", count=7, centroid=run(2300, 5)
    )
    best, distance = fp.match(run(2380, 345), [dryer, washer])
    assert best is washer or best is dryer
    assert best.label == "Dryer"
    assert distance < 0.2


# --- serialisation ---------------------------------------------------------


def test_a_fingerprint_survives_a_round_trip_through_storage():
    original = fp.Fingerprint(
        label="Dryer",
        circuit="sensor.circuit_21_power",
        count=12,
        centroid=run(2400, 350),
        spread={"peak_w": 50.0},
        cadence={"runs": 6, "median_gap_s": 3600.0, "p90_gap_s": 5400.0},
    )
    restored = fp.Fingerprint.from_dict(original.to_dict())
    assert restored == original


def test_a_library_learned_before_cadence_existed_still_loads():
    """None means "not characterised", which absence detection answers honestly."""
    restored = fp.Fingerprint.from_dict(
        {"label": "Dryer", "circuit": "sensor.c", "count": 4}
    )
    assert restored.cadence is None
    assert restored.centroid == {}


def test_a_library_written_to_disk_reads_back_the_same(tmp_path):
    path = str(tmp_path / "library.json")
    shapes = [
        fp.Fingerprint(
            label="Dryer", circuit="sensor.c", count=9, centroid=run(2400, 350)
        )
    ]
    fp.save_library(shapes, path)
    assert json.loads(pathlib.Path(path).read_text(encoding="utf-8"))[0]["label"] == (
        "Dryer"
    )
    assert fp.load_library(path) == shapes


def test_a_missing_or_corrupt_library_reads_as_empty(tmp_path):
    """A tool run against a machine with no library yet is ordinary, not a fault."""
    assert fp.load_library(str(tmp_path / "nothing.json")) == []
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert fp.load_library(str(broken)) == []
