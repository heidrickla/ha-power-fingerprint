"""The labelled-set tool: where its labels come from, and that its scoring is fair.

The tool loads the pure modules by path, so this runs on a bare checkout.
"""

import gzip
import importlib.util
import json
import pathlib
import random
import sys

_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "labelled_set.py"
sys.path.insert(0, str(_PATH.parent))
_spec = importlib.util.spec_from_file_location("pf_labelled_set", _PATH)
ls = importlib.util.module_from_spec(_spec)
sys.modules["pf_labelled_set"] = ls
_spec.loader.exec_module(ls)

START = 1_788_000_000.0


def _stamp(seconds):
    from datetime import UTC, datetime

    return datetime.fromtimestamp(START + seconds, UTC)


def _run(start, end, label=None, absent=(), split="tune"):
    return {
        "circuit": "c",
        "start": START + start,
        "end": START + end,
        "split": split,
        "features": {},
        "watts": [],
        "label": label,
        "source": None,
        "absent": list(absent),
    }


# --- clustering ------------------------------------------------------------


def test_the_fast_clustering_makes_the_integrations_merges():
    """The matrix version exists for speed only. Ties included: runs that
    round to the same features are common, and tie order decides merges."""
    rng = random.Random(7)
    for n in (1, 2, 17, 60):
        rows = [
            {
                "peak_w": rng.choice([100, 150, 300, 310, 900]) * rng.uniform(0.9, 1.1),
                "floor_w": rng.choice([5, 80, 140]),
                "mean_w": rng.uniform(50, 800),
                "plateaus": rng.randint(1, 6),
                "duty_above_half_peak": round(rng.random(), 1),
            }
            for _ in range(n)
        ]
        rows += rows[: n // 3]  # exact duplicates
        points = ls.fp.normalize(rows)
        for threshold in (0.7, 0.9, 1.2):
            assert ls.complete_linkage(points, threshold) == ls.fp.cluster(
                points, threshold=threshold
            )


# --- labels from a metered device ------------------------------------------


def _device(pairs):
    return ls.Held([(_stamp(t), w) for t, w in pairs])


def test_a_device_switching_with_the_run_labels_it():
    device = _device([(0, 0.0), (1000, 110.0), (1600, 0.0)])
    who, absent = ls.explain(_run(1010, 1590), 100.0, {"outlet": device})
    assert (who, absent) == ("outlet", [])


def test_a_device_already_on_when_the_run_starts_does_not_label_it():
    """Being on during a run is not causing it: a light on all evening
    overlaps every charger burst."""
    device = _device([(0, 0.0), (100, 40.0), (9000, 0.0)])
    who, absent = ls.explain(_run(1000, 1060), 40.0, {"light": device})
    assert (who, absent) == (None, [])


def test_a_device_drawing_far_less_than_the_run_does_not_label_it():
    device = _device([(0, 0.0), (1000, 10.0), (1600, 0.0)])
    who, _absent = ls.explain(_run(1000, 1600), 400.0, {"lamp": device})
    assert who is None


def test_a_device_off_throughout_is_listed_absent():
    device = _device([(0, 0.0), (5000, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"outlet": device}) == (
        None,
        ["outlet"],
    )


def test_a_device_that_had_not_reported_is_neither_label_nor_absent():
    """Unread is not off."""
    device = _device([(5000, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"outlet": device}) == (None, [])


def test_two_devices_explaining_one_run_label_neither():
    a = _device([(0, 0.0), (1000, 100.0), (1600, 0.0)])
    b = _device([(0, 0.0), (1005, 100.0), (1595, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"a": a, "b": b})[0] is None


# --- splits and sums -------------------------------------------------------


def test_splits_fall_on_day_fourteen_and_day_twenty():
    day = ls.DAY
    assert ls.split_of(START + 14 * day - 1, START) == "fit"
    assert ls.split_of(START + 14 * day, START) == "tune"
    assert ls.split_of(START + 20 * day - 1, START) == "tune"
    assert ls.split_of(START + 20 * day, START) == "heldout"


def test_overlap_finds_a_component_run_inside_or_across_the_run():
    spans = [(START + 0, START + 100), (START + 500, START + 900)]
    assert ls.overlaps(_run(50, 60), spans)
    assert ls.overlaps(_run(400, 520), spans)
    assert ls.overlaps(_run(600, 700), spans)
    assert not ls.overlaps(_run(200, 400), spans)
    assert not ls.overlaps(_run(950, 990), spans)


# --- scoring ---------------------------------------------------------------


def test_claiming_an_absent_device_is_wrong():
    run = _run(0, 60, absent=["outlet"])
    assert ls.judge("outlet", run) == "wrong"
    assert ls.judge(None, run) == "negative_ok"
    assert ls.judge("fridge", run) == "negative_ok"


def test_naming_one_side_of_an_overlap_is_partial_and_both_is_correct():
    run = _run(0, 60, label="Oven+Microwave")
    assert ls.judge("Oven", run) == "partial"
    assert ls.judge("Microwave+Oven", run) == "correct"
    assert ls.judge("Dryer", run) == "wrong"
    assert ls.judge(None, run) == "unknown"


def test_a_cluster_is_named_only_when_its_label_outvotes_its_absences():
    fit = [
        _run(0, 1, label="archway"),
        _run(0, 1, label="archway"),
        _run(0, 1, absent=["archway"]),
        _run(0, 1, absent=["archway"]),
        _run(0, 1, absent=["archway"]),
        _run(0, 1, label="Oven"),
        _run(0, 1, label="Oven+Microwave"),
    ]
    names = ls.oracle_names([0, 0, 0, 0, 0, 1, 2], fit)
    assert names == {1: "Oven"}


def test_the_live_view_is_what_the_coordinator_computes():
    """Built incrementally for speed; the coordinator recomputes each poll."""
    rng = random.Random(3)
    watts = [rng.choice([0.0, 60.0, 400.0, 1200.0]) + rng.random() for _ in range(400)]
    views = ls.live_views(watts)
    polled = watts[:: ls.POLL_EVERY]
    for n, view in enumerate(views, start=3):
        seen = polled[:n]
        peak = max(seen)
        assert view == {
            "peak_w": peak,
            "floor_w": min(seen),
            "mean_w": sum(seen) / n,
            "plateaus": len({round(w / 50.0) for w in seen}),
            "duty_above_half_peak": sum(1 for w in seen if w > peak / 2) / n,
        }


def test_the_live_view_starts_at_the_third_poll():
    """The coordinator answers `starting` for the first two polls of a run."""
    views = ls.live_views([100.0] * 5 + [200.0] * 5 + [300.0] * 5)
    assert len(views) == 1
    assert views[0]["peak_w"] == 300.0
    assert views[0]["floor_w"] == 100.0


# --- freezing --------------------------------------------------------------


def test_the_set_is_the_same_bytes_every_build(tmp_path):
    """The hash freezes the set only if building it again gives it back."""
    circuit, device = "sensor.c_power", "sensor.outlet_power"
    rows = []
    for k in range(40):
        base = k * 3600
        rows += [(base + t, 2.0) for t in range(0, 600, 6)]
        rows += [(base + 600 + t, 400.0) for t in range(0, 300, 6)]
        rows += [(base + 900 + t, 2.0) for t in range(0, 600, 6)]
    for name, pairs in (
        (circuit, rows),
        (
            device,
            [(k * 3600 + 600, 398.0) for k in range(40)]
            + [(k * 3600 + 900, 0.0) for k in range(40)],
        ),
    ):
        lines = "".join(f"{START + t:.3f}|{w}\n" for t, w in sorted(pairs))
        (tmp_path / f"{name}.csv.gz").write_bytes(gzip.compress(lines.encode()))
    spec = {
        "start": START,
        "end": START + 40 * 3600,
        "units": {},
        "dedicated": {},
        "devices": {device: {"circuit": circuit, "placed_by": "declared"}},
    }
    first, _ = ls.build(spec, tmp_path)
    second, _ = ls.build(json.loads(json.dumps(spec)), tmp_path)
    assert ls.serialise(first) == ls.serialise(second)
    assert sum(1 for r in first if r["label"] == device) == 40
