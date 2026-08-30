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
    reports on change, an Emporia every ~12 s - so they cannot be compared
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


def step_match(
    device: list[float],
    circuit: list[float],
    min_delta_w: float = 5.0,
    tolerance: float = 0.5,
    slack: int = 1,
) -> float:
    """Fraction of the device's switching events the circuit also shows.

    ⛔ THIS EXISTS BECAUSE CORRELATION FAILS ON SMALL LOADS AND FAILS QUIETLY.
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


def assign(
    devices: dict[str, list[float]],
    circuits: dict[str, list[float]],
    min_r: float = 0.5,
    min_containment: float = 0.98,
    min_step_match: float = 0.35,
    min_margin: float = 0.15,
) -> dict[str, dict[str, float | str | None]]:
    """Work out which circuit each metered device sits on.

    Returns the best candidate per device, or circuit=None when nothing clears
    both bars. Refusing is the right answer surprisingly often: a device that
    never changed state during the window carries no information, and guessing
    a circuit for it would poison everything downstream.
    """
    out: dict[str, dict[str, float | str | None]] = {}
    for dname, dtrace in devices.items():
        scored: list[tuple[str, float, float, float]] = []
        for cname, ctrace in circuits.items():
            c = containment(dtrace, ctrace)
            if c < min_containment:
                continue  # cannot draw more than the circuit feeding it
            sm = step_match(dtrace, ctrace)
            r = pearson(dtrace, ctrace)
            # Either route qualifies: step matching catches small loads on busy
            # circuits, correlation catches loads that dominate their circuit
            # but switch too rarely to give many steps.
            if sm < min_step_match and r < min_r:
                continue
            scored.append((cname, sm, r, c))

        scored.sort(key=lambda row: -max(row[1], row[2]))
        best = scored[0] if scored else None
        runner = scored[1] if len(scored) > 1 else None

        # ⛔ A WINNER MUST BEAT THE RUNNER-UP, NOT MERELY SCORE HIGHEST.
        # Without this, a busy circuit absorbs every weak match. Measured
        # against Home Assistant's own area assignments: taking the top score
        # alone put six devices from THREE different areas - Office, Guest
        # Bathroom and Master Bathroom - on one circuit, which is not plausible
        # wiring. When several circuits score alike, none of them is evidence.
        margin = 0.0
        if best and runner:
            margin = max(best[1], best[2]) - max(runner[1], runner[2])
            if margin < min_margin:
                best = None

        out[dname] = (
            {
                "circuit": best[0],
                "step_match": round(best[1], 3),
                "r": round(best[2], 3),
                "containment": round(best[3], 3),
                "margin": round(margin, 3),
            }
            if best
            else {
                "circuit": None,
                "step_match": 0.0,
                "r": 0.0,
                "containment": 0.0,
                "margin": round(margin, 3),
                "reason": "ambiguous" if scored else "no candidate",
            }
        )
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
