"""Using self-metering devices to bootstrap the fingerprint library.

Most homes with per-circuit monitoring ALSO have a scattering of devices that
meter themselves - smart dimmers, metered outlets, a PDU with per-outlet
reporting. On the development install there are 40 of them alongside 27
circuits.

Those devices are worth far more than one extra reading each, because they are
already labelled. `sensor.front_porch_light_active_power` needs no human to
name it. That gives three things the circuit CTs alone cannot:

1.  GROUND TRUTH FOR FREE. Every metered device is a labelled fingerprint,
   which is the bootstrap problem solved without asking anyone anything.

2.  SUBTRACTION. A metered device sitting on a monitored circuit can be
   removed from that circuit's trace, leaving the appliances that share it.
   This is the honest way to attack a shared circuit - rather than inferring
   two appliances from one blended signal, measure one and subtract it.

3.  AUTOMATIC DEVICE-TO-CIRCUIT MAPPING. Correlating a device's trace against
   every circuit finds which circuit it is wired to. That map is needed by the
   contradiction check anyway, and deriving it beats asking someone to type it.

The correlation deliberately requires BOTH agreement and containment. A device
on circuit A may correlate with circuit B by coincidence - two lights switched
by the same automation move together without sharing a breaker. But a device
cannot draw more power than the circuit carrying it, so containment breaks the
coincidence. Neither test alone is sufficient; that is the whole point.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

Sample = tuple[datetime, float]


def resample(
    samples: list[Sample], step_s: int, start: datetime, end: datetime
) -> list[float]:
    """Put a trace on a fixed grid by holding the last known value forward.

    Traces from different integrations arrive on unrelated cadences - Z-Wave
    reports on change, an Emporia every ~6 s - so they cannot be compared
    point by point without this.
    """
    if not samples:
        return []
    ordered = sorted(samples, key=lambda s: s[0])
    out: list[float] = []
    idx = 0
    last = ordered[0][1]
    t = start
    while t <= end:
        while idx < len(ordered) and ordered[idx][0] <= t:
            last = ordered[idx][1]
            idx += 1
        out.append(last)
        t += timedelta(seconds=step_s)
    return out


def pearson(a: list[float], b: list[float]) -> float:
    """Correlation of two equal-length series. 0.0 when either is flat."""
    n = min(len(a), len(b))
    if n < 3:
        return 0.0
    a, b = a[:n], b[:n]
    ma, mb = sum(a) / n, sum(b) / n
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((x - mb) ** 2 for x in b)
    if va <= 0 or vb <= 0:
        return 0.0
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b, strict=True))
    return cov / math.sqrt(va * vb)


def containment(
    device: list[float], circuit: list[float], margin_w: float = 10.0
) -> float:
    """Fraction of the time the circuit carries at least the device's draw.

    A device physically cannot exceed the circuit feeding it. Coincidental
    correlation - two lamps on one automation, different breakers - fails here,
    which is exactly what it is for.
    """
    n = min(len(device), len(circuit))
    if n == 0:
        return 0.0
    return sum(1 for i in range(n) if circuit[i] + margin_w >= device[i]) / n


def steps(trace: list[float], min_delta_w: float) -> list[tuple[int, float]]:
    """Indices where the trace steps by more than `min_delta_w`, with the size."""
    out = []
    for i in range(1, len(trace)):
        delta = trace[i] - trace[i - 1]
        if abs(delta) >= min_delta_w:
            out.append((i, delta))
    return out


def transitions(device: list[float], min_delta_w: float = 5.0) -> int:
    """How many switching events this device produced in the window.

    Deliberately the same `steps()` call and the same default threshold that
    `step_match` uses, so "no transitions" means exactly "nothing step_match
    could ever have matched" rather than a second, subtly different opinion
    about what counts as movement.
    """
    return len(steps(device, min_delta_w))


def traceable(device: list[float], min_delta_w: float = 5.0) -> bool:
    """Whether this device produced anything to trace during this window.

    A fact about the window, not the device: correlation and step matching both
    work on transitions, and containment alone cannot tell a device with none
    apart from any circuit whose floor clears its draw. Network gear and
    freezers draw plenty differently when they start; they simply are not
    switched. A window containing one power cut places them outright.

    The test is steps, not spread. A light on 3% of the time has an identical
    95th and 5th percentile, and slow thermal drift has a wide one.
    """
    return transitions(device, min_delta_w) > 0


def step_match(
    device: list[float],
    circuit: list[float],
    min_delta_w: float = 5.0,
    tolerance: float = 0.5,
    slack: int = 1,
) -> float:
    """Fraction of the device's switching events the circuit also shows.

     THIS EXISTS BECAUSE CORRELATION FAILS ON SMALL LOADS AND FAILS QUIETLY.
    Pearson r compares whole series, so a 10 W lamp on a circuit that swings
    several hundred watts scores near zero even when it is genuinely on that
    circuit - its contribution is swamped by everything else sharing the
    breaker. Measured: correlation placed only 2 of 20 metered devices, and the
    18 it missed were all small loads on busy circuits.

    Steps are the sensitive test. When a 10.8 W lamp switches on, the circuit
    carrying it steps by about 10.8 W at that moment; a circuit not carrying it
    does not. Magnitude has to match too, not just direction, or every circuit
    busy at that instant would claim the device.

    `slack` allows the circuit's step to land a grid cell either side, since
    the two integrations report on unrelated cadences.
    """
    dsteps = steps(device, min_delta_w)
    if not dsteps:
        return 0.0
    n = min(len(device), len(circuit))
    hits = 0
    for i, delta in dsteps:
        if i >= n:
            continue
        best = 0.0
        for off in range(-slack, slack + 1):
            j = i + off
            if 1 <= j < n:
                cdelta = circuit[j] - circuit[j - 1]
                # Same direction and comparable magnitude.
                if delta * cdelta > 0:
                    ratio = min(abs(cdelta), abs(delta)) / max(abs(cdelta), abs(delta))
                    best = max(best, ratio)
        if best >= 1.0 - tolerance:
            hits += 1
    return hits / len(dsteps)


def _score_one(
    dtrace: list[float],
    circuits: dict[str, list[float]],
    min_r: float,
    min_containment: float,
    min_step_match: float,
    min_margin: float,
) -> tuple[tuple[str, float, float, float] | None, float, int]:
    """Score one device against every circuit. Returns (best, margin, n_candidates)."""
    scored: list[tuple[str, float, float, float]] = []
    for cname, ctrace in circuits.items():
        c = containment(dtrace, ctrace)
        if c < min_containment:
            continue  # cannot draw more than the circuit feeding it
        sm = step_match(dtrace, ctrace)
        r = pearson(dtrace, ctrace)
        # Either route qualifies: step matching catches small loads on busy
        # circuits, correlation catches loads that dominate their circuit but
        # switch too rarely to give many steps.
        if sm < min_step_match and r < min_r:
            continue
        scored.append((cname, sm, r, c))

    scored.sort(key=lambda row: -max(row[1], row[2]))
    best = scored[0] if scored else None
    runner = scored[1] if len(scored) > 1 else None

    # A winner must beat the runner-up, not merely score highest, or a busy
    # circuit absorbs every weak match. When several circuits score alike,
    # none of them is evidence.
    margin = 0.0
    if best and runner:
        margin = max(best[1], best[2]) - max(runner[1], runner[2])
        if margin < min_margin:
            best = None
    return best, margin, len(scored)


def assign(
    devices: dict[str, list[float]],
    circuits: dict[str, list[float]],
    min_r: float = 0.5,
    min_containment: float = 0.98,
    min_step_match: float = 0.35,
    min_margin: float = 0.15,
) -> dict[str, dict[str, float | str | None]]:
    """Work out which circuit each metered device sits on.

     ASSIGNMENT IS ITERATIVE, AND EACH CONFIRMED DEVICE IS SPENT.

    Scoring every device against the raw circuit traces independently lets one
    circuit's step be claimed by several devices at once - nothing consumes it,
    so a busy circuit keeps looking like a plausible home for everything. That
    is the mechanism behind the over-assignment this module keeps fighting.

    So the loop takes the single most confident assignment anywhere, locks it,
    SUBTRACTS that device's trace from that circuit, and re-scores everyone
    against the residual. A step that has already been explained by a confirmed
    device is no longer available to explain a different one. Devices that
    genuinely share a circuit still match, because their own steps survive the
    subtraction - only the claimed contribution is removed.

    Ordering by confidence matters: the strongest evidence is committed first,
    so a marginal match can never consume a step that a near-certain match
    needed. Anything that never clears the bars is returned unplaced, which is
    the right answer far more often than a guess would be.
    """
    out: dict[str, dict[str, float | str | None]] = {}
    residual = {c: list(t) for c, t in circuits.items()}

    # Devices with no transition in the window are set aside BEFORE scoring,
    # not scored and then rejected. Scoring them reports the wrong reason, and
    # worse, one that squeaked past the bars on containment alone would have
    # its trace subtracted from the circuit below, stripping the steps a real
    # device needs.
    remaining = {}
    for dname, dtrace in devices.items():
        if traceable(dtrace):
            remaining[dname] = dtrace
        else:
            out[dname] = {
                "circuit": None,
                "step_match": 0.0,
                "r": 0.0,
                "containment": 0.0,
                "margin": 0.0,
                "reason": "no transition in window",
            }
    order = 0

    while remaining:
        winner: tuple[str, tuple[str, float, float, float], float] | None = None
        for dname, dtrace in remaining.items():
            best, margin, _n = _score_one(
                dtrace, residual, min_r, min_containment, min_step_match, min_margin
            )
            if best is None:
                continue
            score = max(best[1], best[2])
            if winner is None or score > max(winner[1][1], winner[1][2]):
                winner = (dname, best, margin)

        if winner is None:
            break  # nothing left clears the bars

        dname, best, margin = winner
        cname = best[0]
        out[dname] = {
            "circuit": cname,
            "step_match": round(best[1], 3),
            "r": round(best[2], 3),
            "containment": round(best[3], 3),
            "margin": round(margin, 3),
            "order": order,
        }
        residual[cname] = subtract(residual[cname], [remaining[dname]])
        del remaining[dname]
        order += 1

    # Whatever is left never cleared the bars, even against the residuals.
    for dname, dtrace in remaining.items():
        _best, margin, n = _score_one(
            dtrace, residual, min_r, min_containment, min_step_match, min_margin
        )
        out[dname] = {
            "circuit": None,
            "step_match": 0.0,
            "r": 0.0,
            "containment": 0.0,
            "margin": round(margin, 3),
            "reason": "ambiguous" if n else "no candidate",
        }
    return out


def subtract(circuit: list[float], devices: list[list[float]]) -> list[float]:
    """Remove known metered devices from a circuit, leaving the remainder.

    Clamped at zero: metering error between two different devices can make the
    subtraction go slightly negative, and a negative watt reading downstream is
    worse than a zero.
    """
    n = len(circuit)
    out = list(circuit)
    for dev in devices:
        for i in range(min(n, len(dev))):
            out[i] -= dev[i]
    return [max(0.0, v) for v in out]
