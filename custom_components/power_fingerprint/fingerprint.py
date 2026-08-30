"""Learning and matching appliance fingerprints.

Pure stdlib on purpose. Nothing here imports Home Assistant, numpy or
scikit-learn: the whole point of clamping every circuit is that the remaining
problem is small enough not to need them. A circuit sees tens to low hundreds of
events in a week, so an O(n^2) agglomerative pass costs nothing and, unlike
k-means, does not need the number of appliances chosen up front - which is
precisely the thing nobody knows in advance.

Clustering finds the recurring shapes. It cannot name them, and no amount of
cleverness will: naming needs someone who knows what is plugged in. So the
contract is deliberately one-sided - this proposes groups and describes them in
plain English, a human names them once, and the labels persist.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any

# Feature -> (log-scaled?, weight, linear divisor).
#
# Absolute log scaling, not per-circuit z-scoring: z-scoring
# rescales each circuit's own variance to fill the range, so trivial variation
# WITHIN one appliance is inflated into apparent difference BETWEEN appliances.
# Measured: it produced 22 "distinct shapes" on a circuit carrying one load and
# 9 on another carrying two.
#
# Log-power space is already a natural metric and needs no fitting: a
# difference of 0.69 IS a doubling, on any circuit, at any scale. So features
# are scaled absolutely and weighted by how much they actually identify an
# appliance.
#
# The weights encode a real asymmetry. Power levels identify a machine; how
# long it happened to run does not. A dryer's cycle length varies with the
# load, and energy inherits that variance because it is power x duration, so
# both are weighted down hard. Peak and floor carry the signal - and floor most
# of all, since it is what separates two appliances sharing a circuit.
FEATURES: dict[str, tuple[bool, float, float]] = {
    "peak_w": (True, 1.0, 1.0),
    "floor_w": (True, 1.0, 1.0),
    "mean_w": (True, 0.8, 1.0),
    "plateaus": (False, 0.4, 5.0),
    "duty_above_half_peak": (False, 0.4, 1.0),
    # Duration and energy are reported but weighted to zero for clustering:
    # with duration counted, one furnace split into four clusters identical in
    # power and differing only in how long each run lasted. A machine is
    # identified by the power it draws, not how long it was left on.
    "duration_s": (True, 0.0, 1.0),
    "energy_wh": (True, 0.0, 1.0),
}


def _scaled(vec: dict[str, float]) -> list[float]:
    out = []
    for key, (is_log, _weight, divisor) in FEATURES.items():
        val = max(float(vec.get(key, 0.0)), 0.0)
        out.append(math.log1p(val) if is_log else val / divisor)
    return out


def normalize(vectors: list[dict[str, float]]) -> list[list[float]]:
    """Scale features absolutely. Deliberately NOT fitted to the input set - see
    the note above FEATURES. Kept as a function so callers read the same way."""
    return [_scaled(v) for v in vectors]


_WEIGHTS = [w for _, w, _ in FEATURES.values()]


def _dist(a: list[float], b: list[float]) -> float:
    """Weighted Euclidean in scaled space.

    Interpretable: with peak and floor at weight 1.0 in log space, a distance
    of ~0.7 is roughly a doubling of power, which is about where two different
    appliances stop looking like one appliance under varying load.
    """
    return math.sqrt(
        sum(w * (x - y) ** 2 for x, y, w in zip(a, b, _WEIGHTS, strict=True))
    )


def cluster(points: list[list[float]], threshold: float = 0.9) -> list[int]:
    """Agglomerative clustering with complete linkage, cut at `threshold`.

    Complete rather than single linkage on purpose: single linkage chains, and
    a washer whose light loads overlap a dryer's would happily merge the two
    into one blob through a bridge of intermediate events. Complete linkage
    keeps clusters compact, which is what "these are the same appliance" means.

    Returns a cluster id per point, ordered largest cluster first.
    """
    n = len(points)
    if n == 0:
        return []
    groups: list[list[int]] = [[i] for i in range(n)]

    while len(groups) > 1:
        best = None
        best_d = float("inf")
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                d = max(
                    _dist(points[a], points[b]) for a in groups[i] for b in groups[j]
                )
                if d < best_d:
                    best_d, best = d, (i, j)
        if best is None or best_d > threshold:
            break
        i, j = best
        groups[i].extend(groups[j])
        groups.pop(j)

    groups.sort(key=len, reverse=True)
    labels = [0] * n
    for cid, members in enumerate(groups):
        for idx in members:
            labels[idx] = cid
    return labels


@dataclass
class Fingerprint:
    """A learned appliance signature: the centroid of one cluster."""

    label: str
    circuit: str
    count: int
    centroid: dict[str, float] = field(default_factory=dict)
    spread: dict[str, float] = field(default_factory=dict)
    # How often this shape actually ran during the learning window, so absence
    # detection can judge an appliance against its own rhythm rather than
    # against a number someone picked. None when there were too few runs to
    # characterise, which is a real answer and not a missing value.
    cadence: dict[str, float] | None = None

    def describe(self) -> str:
        """Plain English, because a human has to recognise this to name it."""
        c = self.centroid
        mins = c.get("duration_s", 0) / 60.0
        dur = f"{mins:.0f} min" if mins >= 1 else f"{c.get('duration_s', 0):.0f} s"
        bits = [
            f"runs {dur}",
            f"peaks {c.get('peak_w', 0):.0f} W",
            f"holds a {c.get('floor_w', 0):.0f} W floor",
            f"{c.get('plateaus', 0):.0f} power level(s)",
        ]
        if c.get("duty_above_half_peak", 0) > 0.8:
            bits.append("sustained, not spiky")
        elif c.get("duty_above_half_peak", 1) < 0.3:
            bits.append("brief spikes")
        return ", ".join(bits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "circuit": self.circuit,
            "count": self.count,
            "centroid": self.centroid,
            "spread": self.spread,
            "cadence": self.cadence,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Fingerprint:
        return cls(
            label=d["label"],
            circuit=d["circuit"],
            count=int(d.get("count", 0)),
            centroid=d.get("centroid", {}),
            spread=d.get("spread", {}),
            # Absent in libraries learned before cadence existed. None is the
            # right value there - it means "not characterised", which absence
            # detection already knows how to answer honestly.
            cadence=d.get("cadence"),
        )


_NOISE_WORDS = (
    "power",
    "energy",
    "sensor",
)


def suggest_label(friendly_name: str) -> str | None:
    """The appliance name already sitting in a circuit's own title, if any.

     NOT INFERENCE. "EmporiaVue Circuit 15 Dish Washer Power" contains the
    answer; somebody typed it when they clamped the panel. Reading it back is
    free and certain, and it is the difference between a user facing 39 shapes
    called `unnamed_0` and facing the handful that genuinely need a human.

    Returns None when nothing descriptive is left - "EmporiaVue Circuit 25
    Power" names a breaker, not an appliance, and guessing from a number would
    be exactly the confident nonsense this project keeps deleting.
    """
    words = friendly_name.replace("&", " ").split()
    out: list[str] = []
    skip_next_number = False
    for word in words:
        low = word.lower().strip(".,")
        if low.startswith("emporiavue") or low in _NOISE_WORDS:
            continue
        if low == "circuit":
            skip_next_number = True
            continue
        if skip_next_number and low.isdigit():
            continue
        skip_next_number = False
        # A bare number before any real word is still part of the breaker's
        # address ("Circuit 6 & 8"), not part of an appliance's name.
        if low.isdigit() and not out:
            continue
        out.append(word)
    label = " ".join(out).strip()
    return label or None


def summarize(
    circuit: str,
    feature_rows: list[dict[str, float]],
    labels: list[int],
    cadences: dict[int, dict[str, float]] | None = None,
) -> list[Fingerprint]:
    """Turn clustered events into one Fingerprint per cluster.

    `cadences` maps cluster id to that cluster's learned rhythm, which absence
    detection later judges against. It is passed in rather than computed here
    because  `analysis`, `fingerprint`, `attribution` and `verify` are loaded
    BY PATH - by the pure test suite and by `tools/_ha.py` - so a relative
    import between them raises "attempted relative import with no known parent
    package" and breaks both. They stay mutually independent on purpose.
    """
    cadences = cadences or {}
    out: list[Fingerprint] = []
    for cid in sorted(set(labels)):
        members = [f for f, lb in zip(feature_rows, labels, strict=True) if lb == cid]
        if not members:
            continue
        rhythm = cadences.get(cid)
        centroid, spread = {}, {}
        for key in FEATURES:
            vals = sorted(m.get(key, 0.0) for m in members)
            mid = vals[len(vals) // 2]
            centroid[key] = round(mid, 2)
            spread[key] = round((vals[-1] - vals[0]) / 2.0, 2)
        out.append(
            Fingerprint(
                label=f"unnamed_{cid}",
                circuit=circuit,
                count=len(members),
                centroid=centroid,
                spread=spread,
                cadence=rhythm,
            )
        )
    return out


def match(
    features: dict[str, float], library: list[Fingerprint], tolerance: float = 1.0
) -> tuple[Fingerprint | None, float]:
    """Nearest-centroid match, refusing rather than guessing.

    Returns (None, distance) when nothing is within `tolerance`. An unmatched
    event is a real signal - it means something new was plugged in, or an
    existing appliance changed behaviour - so it must not be silently forced
    into the closest bucket.
    """
    if not library:
        return None, float("inf")
    rows = normalize([fp.centroid for fp in library] + [features])
    target = rows[-1]
    best, best_d = None, float("inf")
    for fp, row in zip(library, rows[:-1], strict=True):
        d = _dist(row, target)
        if d < best_d:
            best_d, best = d, fp
    return (best, best_d) if best_d <= tolerance else (None, best_d)


def save_library(fps: list[Fingerprint], path: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([fp.to_dict() for fp in fps], fh, indent=2)


def load_library(path: str) -> list[Fingerprint]:
    try:
        with open(path, encoding="utf-8") as fh:
            return [Fingerprint.from_dict(d) for d in json.load(fh)]
    except (OSError, ValueError):
        return []


def is_named(fp: Fingerprint) -> bool:
    """A candidate is not an identification.

    `unnamed_N` means clustering found a recurring shape and nobody has said
    what it is. Reporting "unnamed_2 is running" is worse than reporting
    nothing, so everything user-facing filters on this.
    """
    return not fp.label.startswith("unnamed_")


def carry_labels(old: list[Fingerprint], new: list[Fingerprint]) -> list[Fingerprint]:
    """Preserve human-applied labels across a re-learn, by position.

    Re-learning over more data must not silently discard naming work. Position
    is the carrier because clustering returns clusters largest-first and a
    re-run over a superset usually preserves that order.

    It is not guaranteed to. When the order does shift, a label lands on the
    wrong shape and the user can see that and fix it. That is the deliberate
    trade: a visible wrong label beats silently throwing the labels away, and
    beats pretending a positional match is an identity match.
    """
    for idx, fp in enumerate(new):
        if idx < len(old) and is_named(old[idx]):
            fp.label = old[idx].label
    return new
