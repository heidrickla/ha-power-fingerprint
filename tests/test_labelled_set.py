"""The labelled-set tool: where its labels come from, and that its scoring is fair.

The tool loads the pure modules by path, so this runs on a bare checkout.
"""

import argparse
import gzip
import importlib.util
import json
import pathlib
import random
import sys
from datetime import UTC, datetime

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "tools" / "labelled_set.py"
sys.path.insert(0, str(_PATH.parent))
_spec = importlib.util.spec_from_file_location("pf_labelled_set", _PATH)
ls = importlib.util.module_from_spec(_spec)
sys.modules["pf_labelled_set"] = ls
_spec.loader.exec_module(ls)

START = 1_788_000_000.0


def _stamp(seconds):
    return datetime.fromtimestamp(START + seconds, UTC)


def _run(start, end, label=None, absent=(), split="tune", circuit="c", **extra):
    return {
        "circuit": circuit,
        "start": START + start,
        "end": START + end,
        "split": split,
        "features": {},
        "watts": [],
        "label": label,
        "source": None,
        "absent": list(absent),
        **extra,
    }


def _write(path, rows):
    lines = "".join(f"{START + t:.3f}|{w}\n" for t, w in rows)
    path.write_bytes(gzip.compress(lines.encode()))


def _cycles(starts, watts, length=300, idle=2.0, step=6):
    """A circuit idling at `idle` with a `length`-second run at each start."""
    rows, t = [], 0
    for s in sorted(starts):
        rows += [(x, idle) for x in range(t, s, step)]
        rows += [(x, watts) for x in range(s, s + length, step)]
        t = s + length
    return rows + [(x, idle) for x in range(t, t + 600, step)]


# --- clustering ------------------------------------------------------------


def _rows(rng, n):
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
    return rows + rows[: n // 3]  # exact duplicates


def test_the_fast_clustering_makes_the_integrations_merges():
    """The matrix version exists for speed only. Ties included: runs that
    round to the same features are common, and tie order decides merges."""
    pytest.importorskip("numpy")
    rng = random.Random(7)
    for n in (1, 2, 17, 60):
        points = ls.fp.normalize(_rows(rng, n))
        for threshold in (0.7, 0.9, 1.2):
            assert ls.complete_linkage(points, threshold) == ls.fp.cluster(
                points, threshold=threshold
            )


def test_without_numpy_the_integrations_own_clustering_runs(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    points = ls.fp.normalize(_rows(random.Random(5), 20))
    assert ls.complete_linkage(points, 0.9) == ls.fp.cluster(points, threshold=0.9)


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


def test_a_device_switching_on_45_s_before_the_run_does_not_label_it():
    device = _device([(0, 0.0), (955, 100.0), (1600, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"outlet": device}) == (None, [])


def test_a_device_left_on_long_after_the_run_does_not_label_it():
    device = _device([(0, 0.0), (1005, 100.0), (3400, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"outlet": device}) == (None, [])


def test_a_long_run_allows_its_end_ten_percent_of_its_length():
    on_until = {"inside": 10800, "outside": 11500}
    for case, off in on_until.items():
        device = _device([(0, 0.0), (10, 100.0), (off, 0.0)])
        who, _absent = ls.explain(_run(0, 10000), 100.0, {"heater": device})
        assert who == ("heater" if case == "inside" else None), case


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


def test_a_device_switching_on_inside_the_run_is_not_absent():
    device = _device([(0, 0.0), (1300, 100.0), (1400, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"outlet": device}) == (None, [])


def test_a_device_that_had_not_reported_is_neither_label_nor_absent():
    """Unread is not off."""
    device = _device([(5000, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"outlet": device}) == (None, [])


def test_an_unread_stretch_is_neither_label_nor_absent():
    """A plug that drops off the network keeps drawing; the last 0 W it
    reported says nothing about the hours it was unread."""
    dropped = _device([(0, 0.0), (900, None), (2000, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"plug": dropped}) == (None, [])
    broken = _device([(0, 0.0), (1000, 100.0), (1300, None), (1600, 0.0)])
    assert ls.explain(_run(1000, 1600), 100.0, {"plug": broken}) == (None, [])


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


def test_two_dedicated_circuits_sum_into_runs_labelled_by_who_ran(tmp_path):
    oven, dryer = "sensor.oven_power", "sensor.dryer_power"
    a_starts = [k * 3600 + 600 for k in range(12)]
    b_starts = [k * 3600 + (700 if k % 4 == 0 else 1800) for k in range(12)]
    _write(tmp_path / f"{oven}.csv.gz", _cycles(a_starts, 1000.0))
    _write(tmp_path / f"{dryer}.csv.gz", _cycles(b_starts, 800.0))
    spec = {
        "start": START,
        "end": START + 12 * 3600 + 1200,
        "units": {oven: "W", dryer: "W"},
        "dedicated": {oven: "Oven", dryer: "Dryer"},
        "devices": {},
    }
    runs, info = ls.build(spec, tmp_path)
    summed = [r for r in runs if r["circuit"].startswith("sum:")]
    assert info["summed_pairs"] == 1
    assert {r["source"] for r in summed} == {"summed"}
    labels = [r["label"] for r in summed]
    assert labels.count("Dryer+Oven") + labels.count("Oven+Dryer") == 3
    assert labels.count("Oven") == 9
    assert labels.count("Dryer") == 9


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


def _shaped(peak, floor, split, label, circuit):
    run = _run(0, 60, label=label, split=split, circuit=circuit)
    run["features"] = {
        "peak_w": peak,
        "floor_w": floor,
        "mean_w": (peak + floor) / 2,
        "plateaus": 2,
        "duty_above_half_peak": 0.9,
    }
    return run


def test_scoring_learns_on_the_fit_part_alone():
    """A shape seen only after fit must never score correct."""
    circuit = "sensor.fit_only"
    runs = [_shaped(1000, 800, "fit", "Kettle", circuit) for _ in range(6)]
    runs += [_shaped(100, 60, "heldout", "Lamp", circuit) for _ in range(4)]
    runs += [_shaped(1000, 800, "heldout", "Kettle", circuit)]
    tally = ls.score_circuit(runs, "heldout", 0.9, ls.baseline)
    assert tally["shapes"] == 1
    assert tally["correct"] == 1
    assert tally["correct"] + tally["wrong"] + tally["unknown"] == 5


def test_a_zero_margin_answers_as_the_baseline():
    rng = random.Random(11)
    shapes = [
        ls.fp.Fingerprint(name, "c", 5, row)
        for name, row in zip(("A", "B", "B", "C"), _rows(rng, 4), strict=False)
    ]
    library = ls.Library(shapes)
    zero = ls.with_margin(0.0)
    for features in _rows(rng, 50):
        assert zero(features, library) == ls.baseline(features, library)


# --- live replay -----------------------------------------------------------


def _live(watts, offsets, threshold=10.0):
    return {"watts": watts, "offsets": offsets, "threshold_w": threshold}


def test_the_live_view_is_live_features_at_each_poll():
    """Built incrementally for speed; the coordinator recomputes each poll."""
    rng = random.Random(3)
    offsets = [6.0 * i for i in range(400)]
    watts = [rng.choice([60.0, 400.0, 1200.0]) + rng.random() for _ in offsets]
    run = _live(watts, offsets)
    polled = ls.polls(run)
    assert polled == watts[::5]
    views = ls.live_views(run)
    assert views == [ls.fp.live_features(polled[:n]) for n in range(3, len(polled) + 1)]


def test_a_poll_at_or_below_the_threshold_starts_the_run_again():
    """`_match_live` clears its samples at an idle read, so a washer's dip
    never becomes its floor."""
    watts = [500.0, 500.0, 500.0, 5.0, 500.0, 500.0, 500.0]
    views = ls.live_views(_live(watts, [30.0 * i for i in range(7)], 50.0))
    assert len(views) == 2
    assert views[1]["floor_w"] == 500.0


def test_polls_are_thirty_seconds_apart_whatever_the_meter_cadence():
    offsets = [float(i) for i in range(121)]
    run = _live([float(i) + 100.0 for i in range(121)], offsets)
    assert ls.polls(run) == [100.0, 130.0, 160.0, 190.0, 220.0]
    assert len(ls.live_views(run)) == 3


# --- freezing --------------------------------------------------------------


def _device_circuit(tmp_path, starts, device_starts=None):
    circuit, device = "sensor.c_power", "sensor.outlet_power"
    _write(tmp_path / f"{circuit}.csv.gz", _cycles(starts, 400.123456789))
    ons = device_starts if device_starts is not None else starts
    _write(
        tmp_path / f"{device}.csv.gz",
        sorted([(s, 398.0) for s in ons] + [(s + 300, 0.0) for s in ons]),
    )
    spec = {
        "start": START,
        "end": START + max(starts) + 2000,
        "units": {circuit: "W", device: "W"},
        "dedicated": {},
        "devices": {device: {"circuit": circuit, "placed_by": "declared"}},
    }
    return spec, device


def test_the_set_is_the_same_bytes_every_build(tmp_path):
    """The hash freezes the set only if building it again gives it back."""
    spec, device = _device_circuit(tmp_path, [k * 3600 + 600 for k in range(40)])
    first, _ = ls.build(spec, tmp_path)
    second, _ = ls.build(json.loads(json.dumps(spec)), tmp_path)
    assert ls.serialise(first) == ls.serialise(second)
    assert sum(1 for r in first if r["label"] == device) == 40
    assert 400.123456789 in first[0]["watts"]  # stored as read, not rounded


def test_the_shift_control_counts_what_coincidence_alone_labels(tmp_path):
    starts = [k * 5000 + (k * k * 397) % 2500 for k in range(40)]
    spec, device = _device_circuit(tmp_path, starts)
    _runs, info = ls.build(spec, tmp_path)
    by_shift = info["device_labels_by_shift"][device]
    assert by_shift["0h"] == 40
    assert all(by_shift[f"{h:g}h"] < 10 for h in ls.SHIFTS_H)


def test_a_missing_unit_stops_the_build(tmp_path):
    spec, device = _device_circuit(tmp_path, [600, 4200])
    del spec["units"][device]
    with pytest.raises(SystemExit, match=device):
        ls.build(spec, tmp_path)


def test_a_malformed_line_stops_the_build_and_names_it(tmp_path):
    spec, device = _device_circuit(tmp_path, [600, 4200])
    for bad in (f"{START:.3f},5.0", f"{START:.3f}|on"):
        (tmp_path / f"{device}.csv.gz").write_bytes(gzip.compress(bad.encode()))
        with pytest.raises(SystemExit, match=r"outlet_power\.csv\.gz:1"):
            ls.build(spec, tmp_path)


def test_the_hash_check_refuses_an_edited_set(tmp_path):
    spec, _device = _device_circuit(tmp_path, [k * 3600 + 600 for k in range(5)])
    (tmp_path / "spec.json").write_text(json.dumps(spec), encoding="utf-8")
    out = tmp_path / "set"
    args = argparse.Namespace(spec=tmp_path / "spec.json", traces=tmp_path, out=out)
    assert ls.cmd_build(args) == 0
    runs, _meta = ls.load_set(out / "set.jsonl.gz")
    assert len(runs) == 5
    raw = gzip.decompress((out / "set.jsonl.gz").read_bytes())
    edited = raw.replace(b'"split": "fit"', b'"split": "heldout"', 1)
    assert edited != raw
    (out / "set.jsonl.gz").write_bytes(gzip.compress(edited))
    with pytest.raises(SystemExit, match="does not match"):
        ls.load_set(out / "set.jsonl.gz")
