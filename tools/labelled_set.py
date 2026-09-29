"""Build a labelled set of circuit runs from recorder history, and score matching on it.

    python tools/labelled_set.py build --spec spec.json --traces DIR --out DIR
    python tools/labelled_set.py report --set DIR/set.jsonl.gz
    python tools/labelled_set.py score --set DIR/set.jsonl.gz --split tune
    python tools/labelled_set.py stability --trace DIR/<entity_id>.csv.gz

Labels never come from the matcher. Three sources, and one derived from the first:

| Source | Runs labelled | Label |
|---|---|---|
| `dedicated` | on a breaker that feeds one appliance | the appliance |
| `declared` | by a metered device its owner placed there | the device, or absent |
| `probe` | as `declared`, for a device a probe placed | the device, or absent |
| `summed` | on the sum of two dedicated traces | `A`, `B`, or `A+B` |

A device explains a run when one of its own on-stretches starts within 30 s of
the run and ends within 60 s or 10% of its length, and its mean draw is 0.5-1.5
times the run's mean above the circuit's pre-run baseline. A run the device
read at or below 5 W for throughout lists it under `absent`; a device unread at
any point of the run is neither. The same rule with each device trace shifted
by 3, 7, 13 and 19 hours gives the count coincidence alone labels; `set.json`
carries both.

Splits are by run start: `fit` the first 14 days, `tune` the next 6, `heldout`
the rest. The on/off threshold of every trace comes from its fit part alone.

`score --live` replays each run as the coordinator polls it: a hold-last read
every `POLL_SECONDS` from the run's start, a reset at any read at or below the
threshold, and a match from the third consecutive read above it. The threshold
is the fit threshold, where the coordinator takes one from its 24-hour window.

Traces are `<entity_id>.csv.gz` files of `<unix seconds>|<state>` lines. This
recorder query returns the rows the learn action reads:

    SELECT printf('%.3f', last_updated_ts) || '|' || ifnull(state, '')
    FROM states
    WHERE metadata_id = (SELECT metadata_id FROM states_meta
                         WHERE entity_id = '<entity_id>')
      AND last_updated_ts >= <start> AND last_updated_ts < <end>
      AND (last_changed_ts IS NULL OR last_changed_ts = last_updated_ts)
    ORDER BY last_updated_ts;

`unavailable`, `unknown` and empty states are unread time; the recorder writes a
null state when an entity is removed or renamed. Any other state that is not a
number, or a line without `|`, stops the build. Every entity needs a power unit
in the spec.

The spec names the window, the units and the ground truth:

    {"start": 1788044400, "end": 1790636400,
     "units": {"<entity_id>": "W"},
     "dedicated": {"<circuit>": "<appliance>"},
     "devices": {"<device sensor>": {"circuit": "<circuit>",
                                     "placed_by": "declared" | "probe"}}}

`set.jsonl.gz` is frozen by the SHA-256 of its uncompressed bytes, written to
`set.json` beside it with the counts. `score` checks the hash before reading.
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import gzip
import hashlib
import io
import itertools
import json
import os
import random
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _ha

an = _ha.load_module("analysis")
fp = _ha.load_module("fingerprint")
at = _ha.load_module("attribution")
const = _ha.load_module("const")

DAY = 86400.0
FIT_DAYS, TUNE_DAYS = 14, 6
EXPLAIN_RATIO = (0.5, 1.5)
START_S, END_S = 30.0, 60.0
SHIFTS_H = (3.0, 7.0, 13.0, 19.0)
DEVICE_ON_W = 5.0
BASELINE_S = 300.0
SUM_GRID_S = 6
MIN_RUNS_TO_SUM = 10
UNREAD = frozenset({"unavailable", "unknown", ""})
Sample = tuple[datetime, float]
Reading = tuple[datetime, float | None]
Run = dict[str, Any]


# --- traces -----------------------------------------------------------------


def read_trace(path: Path, unit: str | None) -> list[Reading]:
    """A trace in watts, with None for unread time. A malformed line stops it."""
    if an.to_watts(1.0, unit) is None:
        raise SystemExit(f"{path.name}: {unit!r} is not a power unit")
    out: list[Reading] = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            stamp, sep, raw = line.rstrip("\n").partition("|")
            try:
                when = datetime.fromtimestamp(float(stamp), UTC)
                if not sep:
                    raise ValueError("no | separator")
                out.append(
                    (when, None if raw in UNREAD else an.to_watts(float(raw), unit))
                )
            except ValueError as err:
                raise SystemExit(f"{path.name}:{number}: {err}: {line!r}") from None
    if not any(w is not None for _t, w in out):
        raise SystemExit(f"{path.name}: no reading in the file")
    return out


def numeric(readings: list[Reading]) -> list[Sample]:
    """The readings a circuit's history gives the learn action: numbers only."""
    return [(t, w) for t, w in readings if w is not None]


def split_of(start: float, window_start: float) -> str:
    day = (start - window_start) / DAY
    if day < FIT_DAYS:
        return "fit"
    return "tune" if day < FIT_DAYS + TUNE_DAYS else "heldout"


def fit_part(samples: list[Sample], window_start: float) -> list[Sample]:
    cut = window_start + FIT_DAYS * DAY
    return [s for s in samples if s[0].timestamp() < cut]


def runs_of(
    circuit: str, samples: list[Sample], window_start: float
) -> tuple[list[Run], float]:
    """Segment with the integration's own code and a threshold from the fit part."""
    fit = fit_part(samples, window_start)
    threshold = an.on_threshold(fit) if fit else an.on_threshold(samples)
    times = [t for t, _w in samples]
    runs: list[Run] = []
    for event in an.segment(samples, threshold_w=threshold):
        lo = bisect.bisect_left(times, event.start)
        hi = bisect.bisect_right(times, event.end)
        if hi - lo != len(event.samples):
            raise SystemExit(f"{circuit}: repeated timestamps at {event.start}")
        start = event.start.timestamp()
        runs.append(
            {
                "circuit": circuit,
                "start": round(start, 3),
                "end": round(event.end.timestamp(), 3),
                "split": split_of(start, window_start),
                "threshold_w": threshold,
                "features": event.as_features(),
                "watts": event.samples,
                "offsets": [round(t.timestamp() - start, 3) for t in times[lo:hi]],
                "label": None,
                "source": None,
                "absent": [],
            }
        )
    return runs, threshold


# --- labelling --------------------------------------------------------------


class Held:
    """A sparse trace read hold-last, as the state machine reads it, with the
    stretches it spends above `DEVICE_ON_W`. None is unread time."""

    def __init__(self, samples: list[Reading], shift_s: float = 0.0) -> None:
        self.times = [s[0].timestamp() + shift_s for s in samples]
        self.watts = [s[1] for s in samples]
        self.spans: list[tuple[float, float, float]] = []
        start: float | None = None
        drawn: list[float] = []
        for stamp, watts in zip(self.times, self.watts, strict=True):
            if watts is None:
                start = None  # a stretch whose end was not seen is no evidence
            elif watts > DEVICE_ON_W:
                if start is None:
                    start, drawn = stamp, []
                drawn.append(watts)
            elif start is not None:
                self.spans.append((start, stamp, statistics.fmean(drawn)))
                start = None
        self.starts = [s for s, _e, _w in self.spans]

    def at(self, stamp: float) -> float | None:
        """The reading in force at `stamp`, None when unread or not yet reported."""
        i = bisect.bisect_right(self.times, stamp) - 1
        return self.watts[i] if i >= 0 else None

    def off_throughout(self, start: float, end: float) -> bool:
        """Read, and at or below `DEVICE_ON_W`, from `start` to `end`."""
        first = self.at(start)
        i = bisect.bisect_right(self.times, start)
        j = bisect.bisect_right(self.times, end)
        return first is not None and all(
            w is not None and w <= DEVICE_ON_W for w in [first, *self.watts[i:j]]
        )

    def coincides(self, start: float, end: float, low: float, high: float) -> bool:
        """An on-stretch that starts and ends with the run, drawing low-high W."""
        slack = max(END_S, 0.1 * (end - start))
        i = bisect.bisect_left(self.starts, start - START_S)
        while i < len(self.spans) and self.spans[i][0] <= start + START_S:
            s_end, drawn = self.spans[i][1], self.spans[i][2]
            if abs(s_end - end) <= slack and low <= drawn <= high:
                return True
            i += 1
        return False


def excess_of(run: Run, circuit: Held, threshold: float, floor: float) -> float:
    """The run's mean above the circuit's level in the minutes before it."""
    lo = bisect.bisect_left(circuit.times, run["start"])
    hi = bisect.bisect_right(circuit.times, run["end"])
    first = bisect.bisect_left(circuit.times, run["start"] - BASELINE_S)
    before = [w for w in circuit.watts[first:lo] if w <= threshold]
    baseline = statistics.median(before) if before else floor
    return statistics.fmean(circuit.watts[lo:hi]) - baseline


def explain(
    run: Run, excess: float, devices: dict[str, Held]
) -> tuple[str | None, list[str]]:
    """Which device explains a run, and which devices were off throughout."""
    explained: list[str] = []
    absent: list[str] = []
    low, high = EXPLAIN_RATIO
    for name, trace in devices.items():
        if excess > 0 and trace.coincides(
            run["start"], run["end"], low * excess, high * excess
        ):
            explained.append(name)
        elif trace.off_throughout(run["start"], run["end"]):
            absent.append(name)
    return (explained[0] if len(explained) == 1 else None), sorted(absent)


def overlaps(run: Run, spans: list[tuple[float, float]]) -> bool:
    """Whether any of one circuit's runs intersects this run.

    A circuit's runs are disjoint and sorted, so the last one to start before
    this run ends is the only one that can reach back into it.
    """
    i = bisect.bisect_right(spans, (run["end"], float("inf")))
    return i > 0 and spans[i - 1][1] >= run["start"]


def summed_runs(
    a: str,
    b: str,
    traces: dict[str, list[Sample]],
    spans: dict[str, list[tuple[float, float]]],
    labels: dict[str, str],
    window: tuple[float, float],
) -> list[Run]:
    """Runs on the sum of two dedicated circuits, labelled by which one ran."""
    start = datetime.fromtimestamp(window[0], UTC)
    end = datetime.fromtimestamp(window[1] - SUM_GRID_S, UTC)
    grid_a = at.resample(traces[a], SUM_GRID_S, start, end)
    grid_b = at.resample(traces[b], SUM_GRID_S, start, end)
    stamps = [
        datetime.fromtimestamp(window[0] + i * SUM_GRID_S, UTC)
        for i in range(min(len(grid_a), len(grid_b)))
    ]
    total = [(t, x + y) for t, x, y in zip(stamps, grid_a, grid_b, strict=False)]
    name = f"sum:{labels[a]}+{labels[b]}"
    runs, _threshold = runs_of(name, total, window[0])
    for run in runs:
        ran = [labels[c] for c in (a, b) if overlaps(run, spans[c])]
        if len(ran) == 1:
            run["label"] = ran[0]
        elif len(ran) == 2:
            run["label"] = "+".join(ran)
        run["source"] = "summed" if ran else None
    return runs


def build(spec: dict[str, Any], trace_dir: Path) -> tuple[list[Run], dict[str, Any]]:
    window = (float(spec["start"]), float(spec["end"]))
    units: dict[str, str] = spec["units"]
    dedicated: dict[str, str] = spec.get("dedicated", {})
    devices: dict[str, dict[str, str]] = spec.get("devices", {})
    circuits = sorted({*dedicated, *(d["circuit"] for d in devices.values())})
    missing = sorted(e for e in (*circuits, *devices) if e not in units)
    if missing:
        raise SystemExit(f"no unit in the spec for {', '.join(missing)}")

    def load(entity: str) -> list[Reading]:
        return read_trace(trace_dir / f"{entity}.csv.gz", units[entity])

    traces = {c: numeric(load(c)) for c in circuits}
    device_samples = {d: load(d) for d in devices}
    out: list[Run] = []
    spans: dict[str, list[tuple[float, float]]] = {}
    control: dict[str, dict[str, int]] = {}
    for circuit in circuits:
        runs, threshold = runs_of(circuit, traces[circuit], window[0])
        spans[circuit] = [(r["start"], r["end"]) for r in runs]
        if circuit in dedicated:
            for run in runs:
                run["label"], run["source"] = dedicated[circuit], "dedicated"
        here = [d for d, v in devices.items() if v["circuit"] == circuit]
        if not here or circuit in dedicated:
            out.extend(runs)
            continue
        held = Held(traces[circuit])
        floor = an.circuit_floor(fit_part(traces[circuit], window[0]))
        excess = [excess_of(run, held, threshold, floor) for run in runs]
        for shift in (0.0, *SHIFTS_H):
            moved = {d: Held(device_samples[d], shift * 3600.0) for d in here}
            tally: Counter[str] = Counter()
            for run, extra in zip(runs, excess, strict=True):
                who, absent = explain(run, extra, moved)
                tally[who or ""] += 1
                if shift == 0.0:
                    run["absent"] = absent
                    if who is not None:
                        run["label"], run["source"] = who, devices[who]["placed_by"]
            for d in here:
                control.setdefault(d, {})[f"{shift:g}h"] = tally[d]
        out.extend(runs)
    busy = [c for c in dedicated if len(spans.get(c, [])) >= MIN_RUNS_TO_SUM]
    for a, b in itertools.combinations(sorted(busy), 2):
        out.extend(summed_runs(a, b, traces, spans, dedicated, window))
    out.sort(key=lambda r: (r["circuit"], r["start"]))
    return out, {
        "circuits": circuits,
        "summed_pairs": len(busy) * (len(busy) - 1) // 2,
        "device_labels_by_shift": control,
    }


def serialise(runs: list[Run]) -> bytes:
    return "".join(json.dumps(r, sort_keys=True) + "\n" for r in runs).encode()


def counts(runs: Iterable[Run]) -> dict[str, dict[str, dict[str, int]]]:
    """Labelled runs per source, circuit and split, plus `absent`-only runs."""
    table: dict[str, dict[str, dict[str, int]]] = defaultdict(
        lambda: defaultdict(Counter)
    )
    for run in runs:
        if run["label"] is not None:
            table[run["source"]][run["circuit"]][run["split"]] += 1
        elif run["absent"]:
            table["absent only"][run["circuit"]][run["split"]] += 1
        else:
            table["unlabelled"][run["circuit"]][run["split"]] += 1
    return {s: {c: dict(v) for c, v in sorted(t.items())} for s, t in table.items()}


def load_set(path: Path) -> tuple[list[Run], dict[str, Any]]:
    raw = gzip.decompress(path.read_bytes())
    meta = json.loads(path.with_name("set.json").read_text(encoding="utf-8"))
    digest = hashlib.sha256(raw).hexdigest()
    if digest != meta["sha256"]:
        raise SystemExit(f"set hash {digest} does not match set.json {meta['sha256']}")
    return [json.loads(line) for line in io.StringIO(raw.decode())], meta


# --- matching candidates ------------------------------------------------------


def complete_linkage(points: list[list[float]], threshold: float) -> list[int]:
    """`fingerprint.cluster` with a distance matrix, for sets it is too slow on.

    The same merges in the same order: the first closest pair of groups in list
    order, the later group appended to the earlier. Without numpy it calls
    `fingerprint.cluster` itself.
    """
    n = len(points)
    if n == 0:
        return []
    try:
        import numpy as np
    except ImportError:
        return list(fp.cluster(points, threshold))
    x = np.array(points) * np.sqrt(np.array(fp._WEIGHTS))
    dist = np.sqrt(((x[:, None, :] - x[None, :, :]) ** 2).sum(-1))
    np.fill_diagonal(dist, np.inf)
    # Slot k is the group first built from point k. Live slots in ascending
    # order are the list `fingerprint.cluster` keeps, and the first minimum of
    # the symmetric matrix in row-major order is its first closest pair i < j.
    members: dict[int, list[int]] = {i: [i] for i in range(n)}
    while len(members) > 1:
        i, j = divmod(int(np.argmin(dist)), n)
        if dist[i, j] > threshold:
            break
        members[i].extend(members.pop(j))
        merged = np.maximum(dist[i], dist[j])
        dist[i], dist[:, i] = merged, merged
        dist[i, i] = np.inf
        dist[j, :], dist[:, j] = np.inf, np.inf
    groups = [members[k] for k in sorted(members)]
    groups.sort(key=len, reverse=True)
    labels = [0] * n
    for cid, members in enumerate(groups):
        for idx in members:
            labels[idx] = cid
    return labels


def learn(
    rows: list[dict[str, float]], threshold: float
) -> tuple[list[int], list[Any]]:
    labels = complete_linkage(fp.normalize(rows), threshold)
    return labels, fp.summarize("c", rows, labels)


def oracle_names(labels: list[int], fit: list[Run]) -> dict[int, str]:
    """Name each cluster as a person who knew every labelled run would.

    The majority single-appliance label among its labelled members, provided
    those members outnumber the ones that label is `absent` from. An overlap
    label names nothing: a person would not recognise a combination.
    """
    names: dict[int, str] = {}
    for cid in set(labels):
        members = [r for r, lb in zip(fit, labels, strict=True) if lb == cid]
        votes = Counter(
            r["label"] for r in members if r["label"] and "+" not in r["label"]
        )
        if not votes:
            continue
        top, n = votes.most_common(1)[0]
        against = sum(1 for r in members if top in r["absent"])
        if n > against and n * 2 > sum(votes.values()):
            names[cid] = top
    return names


class Library:
    """Named shapes of one circuit, scaled once, with every sum of two shapes of
    different names for the overlap test."""

    def __init__(self, shapes: list[Any]) -> None:
        self.shapes = shapes
        self.rows = fp.normalize([s.centroid for s in shapes])
        self.pairs = [
            (
                "+".join(sorted((x.label, y.label))),
                fp.normalize([sum_centroid(x.centroid, y.centroid)])[0],
            )
            for x, y in itertools.combinations(shapes, 2)
            if x.label != y.label
        ]

    def ranked(self, features: dict[str, float]) -> list[tuple[float, Any]]:
        target = fp.normalize([features])[0]
        return sorted(
            (
                (fp._dist(r, target), s)
                for r, s in zip(self.rows, self.shapes, strict=True)
            ),
            key=lambda x: x[0],
        )


Matcher = Callable[[dict[str, float], Library], str | None]


def baseline(features: dict[str, float], library: Library) -> str | None:
    """`fingerprint.match` itself, as the coordinator calls it."""
    best, _d = fp.match(features, library.shapes)
    return best.label if best else None


def with_margin(margin: float) -> Matcher:
    """Unknown when a fingerprint of another name is within `margin` of the best."""

    def _match(features: dict[str, float], library: Library) -> str | None:
        ranked = library.ranked(features)
        if not ranked or ranked[0][0] > 1.0:
            return None
        best_d, best = ranked[0]
        rival = next((d for d, f in ranked[1:] if f.label != best.label), None)
        if rival is not None and rival - best_d < margin:
            return None
        return str(best.label)

    return _match


def sum_centroid(a: dict[str, float], b: dict[str, float]) -> dict[str, float]:
    """What two shapes running at once look like: powers add, the floor is the
    higher floor, levels and duty follow the busier shape."""
    return {
        "peak_w": a["peak_w"] + b["peak_w"],
        "floor_w": max(a["floor_w"], b["floor_w"]),
        "mean_w": a["mean_w"] + b["mean_w"],
        "plateaus": max(a["plateaus"], b["plateaus"]),
        "duty_above_half_peak": max(
            a["duty_above_half_peak"], b["duty_above_half_peak"]
        ),
    }


def kind_first(features: dict[str, float], library: Library) -> str | None:
    """Call a run an overlap when the sum of two differently named shapes is
    nearer than any one shape, then match singles as the baseline does."""
    if not library.pairs:
        return baseline(features, library)
    single = library.ranked(features)
    best_single = single[0][0] if single else float("inf")
    target = fp.normalize([features])[0]
    d, both = min((fp._dist(row, target), name) for name, row in library.pairs)
    if d <= 1.0 and d < best_single:
        return both
    return baseline(features, library)


def jaccard(a: set[int], b: set[int]) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def stability(
    rows: list[dict[str, float]],
    labels: list[int],
    threshold: float,
    draws: int = 30,
    seed: int = 0,
) -> dict[int, float]:
    """Clusterwise bootstrap Jaccard: how often each cluster's members stay together.

    Each draw resamples the runs with replacement and reclusters. A cluster
    scores the best Jaccard of its sampled members against any new cluster,
    averaged over the draws that sampled it.
    """
    rng = random.Random(seed)
    n = len(rows)
    members: dict[int, set[int]] = defaultdict(set)
    for i, lb in enumerate(labels):
        members[lb].add(i)
    scores: dict[int, list[float]] = defaultdict(list)
    points = fp.normalize(rows)
    for _ in range(draws):
        picks = sorted({rng.randrange(n) for _ in range(n)})
        new = complete_linkage([points[i] for i in picks], threshold)
        new_sets: dict[int, set[int]] = defaultdict(set)
        for idx, lb in zip(picks, new, strict=True):
            new_sets[lb].add(idx)
        chosen = set(picks)
        for cid, mem in members.items():
            seen = mem & chosen
            if seen:
                scores[cid].append(max(jaccard(seen, s) for s in new_sets.values()))
    return {cid: statistics.fmean(v) if v else 0.0 for cid, v in scores.items()}


STABLE_JACCARD = 0.5


def stable_or_large(
    threshold: float, draws: int
) -> Callable[[list[dict[str, float]], list[int]], set[int]]:
    """Keep a cluster unless it is small and dissolves under resampling.

    0.5 is the usual clusterwise-Jaccard line below which a cluster is read as
    dissolved; small is the library audit's three runs or fewer.
    """

    def _keep(rows: list[dict[str, float]], labels: list[int]) -> set[int]:
        stab = stability(rows, labels, threshold, draws=draws)
        sizes = Counter(labels)
        return {
            c
            for c in set(labels)
            if stab.get(c, 0.0) >= STABLE_JACCARD or sizes[c] > fp.SMALL_SHAPE_RUNS
        }

    return _keep


# --- scoring ----------------------------------------------------------------


def score_circuit(
    runs: list[Run],
    split: str,
    threshold: float,
    matcher: Matcher,
    keep: Callable[[list[dict[str, float]], list[int]], set[int]] | None = None,
    live: bool = False,
) -> dict[str, int]:
    """Learn on fit, name as an oracle would, and match each `split` run once
    whole, or with `live` at every poll the coordinator would make."""
    fit = [r for r in runs if r["split"] == "fit"]
    test = [r for r in runs if r["split"] == split and (r["label"] or r["absent"])]
    tally = Counter(
        {"correct": 0, "wrong": 0, "unknown": 0, "partial": 0, "shapes": 0, "named": 0}
    )
    if not fit or not test:
        return dict(tally)
    rows = [r["features"] for r in fit]
    key = (fit[0]["circuit"], threshold)
    if key not in _LEARNED:
        _LEARNED[key] = learn(rows, threshold)
    labels, shapes = _LEARNED[key]
    names = oracle_names(labels, fit)
    kept = keep(rows, labels) if keep else set(labels)
    named = []
    for cid, shape in enumerate(shapes):
        if cid in names and cid in kept:
            named.append(dataclasses.replace(shape, label=names[cid]))
    library = Library(named)
    tally["shapes"], tally["named"] = len(shapes), len(named)
    for run in test:
        views = live_views(run) if live else [run["features"]]
        for features in views:
            tally[judge(matcher(features, library), run)] += 1
    return dict(tally)


# Clustering one circuit's fit runs is the same for every matcher.
_LEARNED: dict[tuple[str, float], tuple[list[int], list[Any]]] = {}


def judge(pred: str | None, run: Run) -> str:
    truth = run["label"]
    if truth is None:  # absent-only: a claim of an absent device is wrong
        return "wrong" if pred in run["absent"] else "negative_ok"
    if "+" in truth and pred in truth.split("+"):
        return "partial"
    if pred is None:
        return "unknown"
    return "correct" if sorted(pred.split("+")) == sorted(truth.split("+")) else "wrong"


def polls(run: Run) -> list[float]:
    """The hold-last reading at each coordinator poll from the run's start."""
    offsets, watts = run["offsets"], run["watts"]
    step = float(const.POLL_SECONDS)
    return [
        watts[bisect.bisect_right(offsets, k * step) - 1]
        for k in range(int(offsets[-1] // step) + 1)
    ]


def live_views(run: Run) -> list[dict[str, float]]:
    """`fingerprint.live_features` at each poll where the coordinator matches.

    A read at or below the threshold clears the run, as `_match_live` does, and
    matching starts at the third consecutive read above it. Built incrementally
    because a run can last days.
    """
    views = []
    ordered: list[float] = []
    levels: set[int] = set()
    total = carry = 0.0
    for w in polls(run):
        if w <= run["threshold_w"]:
            ordered, levels, total, carry = [], set(), 0.0, 0.0
            continue
        bisect.insort(ordered, w)
        levels.add(round(w / 50.0))
        # `sum()` of floats is Neumaier-compensated; kept the same way so each
        # mean equals `live_features` to the last bit.
        step = total + w
        carry += (total - step) + w if abs(total) >= abs(w) else (w - step) + total
        total = step
        n = len(ordered)
        if n < 3:
            continue
        peak = ordered[-1]
        above = n - bisect.bisect_right(ordered, peak / 2)
        views.append(
            {
                "peak_w": peak,
                "floor_w": ordered[0],
                "mean_w": (total + carry if carry else total) / n,
                "plateaus": len(levels),
                "duty_above_half_peak": above / n,
            }
        )
    return views


def net(t: dict[str, int]) -> int:
    """A wrong answer costs twice a missing one: it looks right and is believed."""
    return t.get("correct", 0) - 2 * t.get("wrong", 0)


def score(
    runs: list[Run],
    split: str,
    threshold: float,
    matcher: Matcher,
    keep: Any = None,
    live: bool = False,
) -> dict[str, dict[str, int]]:
    by: dict[str, list[Run]] = defaultdict(list)
    for run in runs:
        by[run["circuit"]].append(run)
    return {
        c: score_circuit(rs, split, threshold, matcher, keep, live)
        for c, rs in sorted(by.items())
    }


def transitions(runs: list[Run], threshold: float, split: str) -> dict[str, Any]:
    """Which shape follows which, learned on fit, scored on `split`.

    Each later run takes its nearest fit shape. The table's guess for the next
    shape is compared with always guessing the commonest shape.
    """
    fit = [r for r in runs if r["split"] == "fit"]
    later = [r for r in runs if r["split"] == split]
    if len(fit) < 3 or len(later) < 2:
        return {}
    labels, shapes = learn([r["features"] for r in fit], threshold)
    table: dict[str, Counter[str]] = defaultdict(Counter)
    seq = [shapes[lb].label for lb in labels]
    for x, y in itertools.pairwise(seq):
        table[x][y] += 1
    common = Counter(seq).most_common(1)[0][0]
    lib = Library(shapes)
    after = [lib.ranked(r["features"])[0][1].label for r in later]
    hit_table = hit_common = 0
    for x, y in itertools.pairwise(after):
        guess = table[x].most_common(1)[0][0] if table.get(x) else common
        hit_table += guess == y
        hit_common += common == y
    pairs = len(after) - 1
    return {
        "shapes": len(shapes),
        "pairs": pairs,
        "table_hit": round(hit_table / pairs, 3),
        "commonest_hit": round(hit_common / pairs, 3),
    }


# --- commands ---------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    spec_bytes = Path(args.spec).read_bytes()
    spec = json.loads(spec_bytes)
    runs, info = build(spec, Path(args.traces))
    raw = serialise(runs)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write(raw)
    (out / "set.jsonl.gz").write_bytes(buf.getvalue())
    traces = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(args.traces).glob("*.csv.gz"))
    }
    meta = {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
        "trace_sha256": traces,
        "window": [spec["start"], spec["end"]],
        "splits_days": {"fit": FIT_DAYS, "tune": TUNE_DAYS},
        "runs": len(runs),
        **info,
        "counts": counts(runs),
    }
    (out / "set.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    print(f"{len(runs)} runs, sha256 {meta['sha256']}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    _runs, meta = load_set(Path(args.set))
    print(f"sha256 {meta['sha256']}  runs {meta['runs']}")
    for source, circuits in meta["counts"].items():
        total = Counter[str]()
        for per in circuits.values():
            total.update(per)
        print(f"\n{source}: {sum(total.values())} ({dict(total)})")
        for circuit, per in circuits.items():
            print(f"  {circuit}: {per}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    runs, meta = load_set(Path(args.set))
    const = _ha.load_module("const")
    report: dict[str, Any] = {
        "sha256": meta["sha256"],
        "split": args.split,
        "live": args.live,
    }
    for profile in args.profiles:
        thr = float(const.CONFIDENCE_PROFILES[profile]["cluster_threshold"])

        def run(matcher: Matcher, keep: Any = None, t: float = thr) -> Any:
            return score(runs, args.split, t, matcher, keep, args.live)

        results: dict[str, Any] = {"baseline": run(baseline)}
        for m in args.margins:
            results[f"margin {m}"] = run(with_margin(m))
        if args.kind:
            results["kind first"] = run(kind_first)
        if args.stability:
            results["stability"] = run(baseline, stable_or_large(thr, args.draws))
        if args.transitions:
            by: dict[str, list[Run]] = defaultdict(list)
            for r in runs:
                by[r["circuit"]].append(r)
            results["transitions"] = {
                c: transitions(rs, thr, args.split) for c, rs in sorted(by.items())
            }
        report[profile] = results
    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    for profile in args.profiles:
        base = report[profile]["baseline"]
        for name, res in report[profile].items():
            if name == "transitions":
                continue
            tot = Counter[str]()
            for t in res.values():
                tot.update(t)
            worse = [c for c in res if net(res[c]) < net(base[c])]
            better = [c for c in res if net(res[c]) > net(base[c])]
            print(
                f"{profile:9s} {name:12s} correct {tot['correct']:6d} wrong "
                f"{tot['wrong']:5d} unknown {tot['unknown']:6d} partial "
                f"{tot['partial']:5d} net {net(tot):7d} circuits better "
                f"{len(better)} worse {len(worse)}"
            )
    return 0


def cmd_stability(args: argparse.Namespace) -> int:
    """Each shape `learn` would find in the last `--days` of one trace, with
    its bootstrap stability."""
    const = _ha.load_module("const")
    thr = float(const.CONFIDENCE_PROFILES[args.profile]["cluster_threshold"])
    samples = numeric(read_trace(Path(args.trace), args.unit))
    cut = samples[-1][0].timestamp() - args.days * DAY
    window = [s for s in samples if s[0].timestamp() >= cut]
    rows = [e.as_features() for e in an.segment(window)]
    labels, shapes = learn(rows, thr)
    stab = stability(rows, labels, thr, draws=args.draws)
    for cid, shape in enumerate(shapes):
        print(f"{cid:3d} {shape.count:4d} runs  stability {stab[cid]:.2f}  ", end="")
        print(shape.describe())
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--spec", required=True)
    b.add_argument("--traces", required=True)
    b.add_argument("--out", required=True)
    r = sub.add_parser("report")
    r.add_argument("--set", required=True)
    s = sub.add_parser("score")
    s.add_argument("--set", required=True)
    s.add_argument("--split", choices=("tune", "heldout"), required=True)
    s.add_argument("--out", required=True)
    s.add_argument("--profiles", nargs="+", default=["balanced"])
    s.add_argument("--margins", nargs="*", type=float, default=[])
    s.add_argument("--kind", action="store_true")
    s.add_argument("--stability", action="store_true")
    s.add_argument("--transitions", action="store_true")
    s.add_argument("--live", action="store_true", help="score every poll of a run")
    s.add_argument("--draws", type=int, default=30)
    t = sub.add_parser("stability")
    t.add_argument("--trace", required=True)
    t.add_argument("--unit", default="W")
    t.add_argument("--days", type=float, default=7.0)
    t.add_argument("--profile", default="balanced")
    t.add_argument("--draws", type=int, default=30)
    args = ap.parse_args()
    commands = {
        "build": cmd_build,
        "report": cmd_report,
        "score": cmd_score,
        "stability": cmd_stability,
    }
    return commands[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
