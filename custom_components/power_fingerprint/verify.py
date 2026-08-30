"""Active verification: switch a device and watch which circuit moves.

Statistical attribution infers which circuit a device sits on from history. It
works, but it is inference, and it fails quietly on small loads, on busy
circuits, and on devices that always switch together. An active probe settles
the same question by measurement: switch the device, see which circuit steps by
the matching amount, switch it back.

The pure decision logic lives here so it can be tested without touching
anything. The actuation lives in `services.py` and is only ever run when a
person asks for it.

⛔ SAFETY RULES, ENFORCED IN CODE, NOT LEFT TO THE CALLER

Only `switch` and `light` entities may be probed. Locks, alarm panels, covers,
valves, climate, water heaters, sirens and media players are refused outright -
a wrong toggle there ranges from annoying to dangerous, and no attribution
result is worth unlocking a door for.

The prior state is always restored, including when the probe fails partway.
A probe that leaves the house in a different state than it found it is a bug
regardless of what it learned.

⚠ AUTOMATIONS CAN CORRUPT A PROBE, AND WILL NOT ANNOUNCE THEMSELVES.

The device being probed is very often one an automation also controls - a motion
light is both the most useful thing to identify and the most likely to be
switched by something else mid-probe. If motion fires while the light is toggled
off, the automation turns it back on, the circuit step vanishes, and the reading
is wrong rather than missing.

There is no clean way to detect that from here, so it is handled by requiring
repeated probes to AGREE rather than by trying to spot the interference. A
spurious automation action lands on one probe and not the other, so the probes
disagree and `agree()` returns nothing. That is the failure mode working: a
corrupted probe should produce no answer, not a confident wrong one.

Probing while the house is quiet reduces it further, but does not remove it.
"""

from __future__ import annotations

# Deliberately a short allowlist rather than a denylist. A denylist silently
# permits every new domain Home Assistant adds; an allowlist fails closed.
PROBE_ALLOWED_DOMAINS = frozenset({"switch", "light"})

# Below this, a device cannot be told apart from meter noise on any circuit.
MIN_PROBE_WATTS = 5.0


def may_probe(entity_id: str) -> tuple[bool, str]:
    """Whether this entity is safe to switch for a measurement."""
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if domain not in PROBE_ALLOWED_DOMAINS:
        return False, (
            f"{entity_id} is a {domain!r} entity; only "
            f"{sorted(PROBE_ALLOWED_DOMAINS)} may be probed"
        )
    return True, ""


def rank_deltas(
    baseline: dict[str, float],
    active: dict[str, float],
    expected_w: float,
    tolerance: float = 0.45,
    noise_w: float = 3.0,
) -> list[dict[str, float | str]]:
    """Rank circuits by how well their change matches the device's own change.

    `expected_w` is what the DEVICE reported it drew. Requiring the circuit's
    step to match that magnitude - not merely to move in the right direction -
    is what stops an unrelated load that happened to switch during the probe
    from claiming the device.
    """
    rows: list[dict[str, float | str]] = []
    for circuit, before in baseline.items():
        after = active.get(circuit)
        if after is None:
            continue
        delta = after - before
        if abs(delta) < noise_w:
            continue
        if delta * expected_w <= 0:
            continue  # moved the wrong way
        ratio = min(abs(delta), abs(expected_w)) / max(abs(delta), abs(expected_w))
        rows.append(
            {"circuit": circuit, "delta_w": round(delta, 1), "match": round(ratio, 3)}
        )
    rows.sort(key=lambda r: -float(r["match"]))
    return [r for r in rows if float(r["match"]) >= 1.0 - tolerance]


def decide(
    ranked: list[dict[str, float | str]], min_margin: float = 0.2
) -> tuple[str | None, str]:
    """Pick a winner, or explain why there is not one.

    Same rule as the statistical path: a winner must beat the runner-up. If two
    circuits both moved by about the device's draw, the probe did not identify
    anything - something else switched at the same moment - and saying so is
    more useful than picking the larger number.
    """
    if not ranked:
        return None, "no circuit moved by the expected amount"
    if len(ranked) == 1:
        return str(ranked[0]["circuit"]), "sole circuit matching the expected step"
    margin = float(ranked[0]["match"]) - float(ranked[1]["match"])
    if margin < min_margin:
        return None, (
            f"ambiguous: {ranked[0]['circuit']} and {ranked[1]['circuit']} "
            f"both moved by about the expected amount"
        )
    return str(ranked[0]["circuit"]), f"clear winner by {margin:.2f}"


def agree(observations: list[str | None]) -> tuple[str | None, str]:
    """Combine repeated probes. All of them must agree.

    A single toggle can coincide with a fridge starting. Two or three that all
    name the same circuit cannot, and requiring unanimity rather than a majority
    keeps the failure mode honest: disagreement returns nothing rather than the
    most popular guess.
    """
    seen = [o for o in observations if o]
    if not seen:
        return None, "no probe identified a circuit"
    if len(seen) != len(observations):
        return None, f"only {len(seen)}/{len(observations)} probes identified a circuit"
    if len(set(seen)) != 1:
        return None, f"probes disagreed: {sorted(set(seen))}"
    return seen[0], f"{len(seen)}/{len(observations)} probes agreed"


# Confidence levels, strongest first. Passive and active are complementary
# rather than alternatives: passive covers every device that ever switches, for
# free, across days of history and without touching anything; active settles the
# cases passive cannot, but costs a real toggle and only answers about one
# device at a time. Agreement between them is worth more than either alone.
CONFIRMED = "confirmed"  # both methods, same circuit
MEASURED = "measured"  # active only
INFERRED = "inferred"  # passive only
CONFLICT = "conflict"  # they disagree - trust neither
UNKNOWN = "unknown"  # neither could tell


def combine(passive: str | None, active: str | None) -> tuple[str | None, str]:
    """Merge a passive inference and an active measurement into one verdict.

    ⛔ DISAGREEMENT RETURNS NOTHING RATHER THAN PREFERRING THE MEASUREMENT.
    It is tempting to let active win, since it is a measurement and passive is
    only inference. But a disagreement means one of them is wrong about a
    physical fact that cannot be both ways, and which one is wrong is not
    knowable from here - the probe may have coincided with another load, or the
    history may be dominated by a device that shares the circuit. Reporting a
    conflict is information; silently preferring one method hides a real
    problem behind a confident answer.
    """
    if passive and active:
        if passive == active:
            return passive, CONFIRMED
        return None, CONFLICT
    if active:
        return active, MEASURED
    if passive:
        return passive, INFERRED
    return None, UNKNOWN
