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
    expected_w: float | None,
    tolerance: float = 0.45,
    noise_w: float = 3.0,
) -> list[dict[str, float | str]]:
    """Rank circuits by how well their change matches the device's own change.

    `expected_w` is what the DEVICE reported it drew. Requiring the circuit's
    step to match that magnitude - not merely to move in the right direction -
    is what stops an unrelated load that happened to switch during the probe
    from claiming the device.

    ⚠ `expected_w` MAY BE None, AND THAT IS NOT AN ERROR. Measured on a live
    panel: a Z-Wave dimmer's own power meter reported no change at all across a
    25-second probe, because Z-Wave devices commonly report power on a slow
    interval or only on significant change, while the circuit CT reports every
    12 seconds. Insisting on the device's own figure made the probe useless for
    exactly the devices most worth identifying.

    With no expected magnitude, ranking falls back to "which circuit moved
    most". That is weaker evidence - it cannot reject an unrelated load of
    similar size - so callers should record that the device was unmetered and
    weight the result accordingly.
    """
    rows: list[dict[str, float | str]] = []
    for circuit, before in baseline.items():
        after = active.get(circuit)
        if after is None:
            continue
        delta = after - before
        if abs(delta) < noise_w:
            continue
        if expected_w is None:
            # No magnitude to compare against; rank by size of movement.
            rows.append(
                {
                    "circuit": circuit,
                    "delta_w": round(delta, 1),
                    "match": 0.0,
                    "unmetered": True,
                }
            )
            continue
        if delta * expected_w <= 0:
            continue  # moved the wrong way
        ratio = min(abs(delta), abs(expected_w)) / max(abs(delta), abs(expected_w))
        rows.append(
            {"circuit": circuit, "delta_w": round(delta, 1), "match": round(ratio, 3)}
        )
    if expected_w is None:
        rows.sort(key=lambda r: -abs(float(r["delta_w"])))
        return rows
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
    """Combine repeated probes. Every probe that ANSWERED must give the same
    circuit, and at least one must have answered.

    ⛔ SILENCE IS NOT DISAGREEMENT, AND CONFLATING THEM DISCARDS GOOD DATA.
    An earlier version demanded unanimity across all probes including the
    silent ones. Measured on a live panel: probing a Foyer light, the first
    probe returned nothing because the device's own Z-Wave meter had not
    reported yet, and the second cleanly identified circuit 30 - matching the
    passive result exactly. Unanimity threw that confirmation away and reported
    no answer at all.

    A probe that produced no reading is MISSING DATA. A probe that named a
    different circuit is a CONTRADICTION. Only the second should void the
    result; the first should reduce confidence, which is what the caller does
    with the returned counts.
    """
    answered = [o for o in observations if o]
    if not answered:
        return None, "no probe identified a circuit"
    if len(set(answered)) != 1:
        return None, f"probes disagreed: {sorted(set(answered))}"
    return answered[0], f"{len(answered)}/{len(observations)} probes agreed"


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


SUSPECT = "suspect"  # probes agreed, but an automation moved during the window


def interference(
    before: dict[str, str | None], after: dict[str, str | None]
) -> list[str]:
    """Automations whose `last_triggered` moved while the probe was running.

    ⛔ THE PROBE CANNOT SEE THIS FROM THE POWER TRACE ALONE, AND IT MATTERS.
    The devices most worth identifying are usually motion lights, which are
    exactly the devices an automation is most likely to switch mid-probe. If
    that happens the circuit step vanishes or doubles and the reading is wrong
    rather than missing.

    Observed on a live install: probing a Foyer light at 01:00, its own motion
    automation fired 70 seconds into the probe. Nothing in the power data would
    have revealed that.

    Comparing `last_triggered` either side of the probe catches it directly,
    which is better than inferring it. Any automation that references the
    probed device or a circuit is worth watching.
    """
    moved = []
    for entity, was in before.items():
        now = after.get(entity)
        if now != was:
            moved.append(entity)
    return sorted(moved)


def grade(
    circuit: str | None,
    fired: list[str],
    device_metered: bool,
    answered: int = 1,
    total: int = 1,
) -> tuple[str | None, str]:
    """Final verdict for one active probe, given what else happened.

    An automation firing during the window does not automatically invalidate
    the answer - it may have had nothing to do with the probed circuit - but it
    does mean the result should not be treated as measured. Downgrading to
    `suspect` and naming the automation lets a person judge, which is more
    useful than either silently trusting it or silently discarding it.
    """
    if circuit is None:
        return None, UNKNOWN
    if fired:
        return circuit, SUSPECT
    if not device_metered:
        return circuit, INFERRED  # only "which circuit moved most"
    if answered < total:
        # Agreed, but some probes were silent - real, and worth less than a
        # clean sweep.
        return circuit, INFERRED
    return circuit, MEASURED


# Domains an automation may touch that make it unsafe to pause, even briefly.
# An allowlist is not possible here - an automation can reference anything - so
# this is a denylist, and it is deliberately broad. The cost of leaving one
# automation running during a probe is a slightly worse measurement. The cost of
# pausing the wrong one is a door that does not unlock, an alarm that trips, or
# a leak that goes unannounced.
NEVER_PAUSE_DOMAINS = frozenset(
    {
        "lock",
        "alarm_control_panel",
        "cover",  # garage doors
        "valve",  # water shutoff
        "water_heater",
        "climate",
        "siren",
        "humidifier",
        "vacuum",
        "notify",
        "persistent_notification",
        "device_tracker",  # arrival/departure logic
        "person",
    }
)

# device_class values that mark a sensor as safety-relevant.
NEVER_PAUSE_DEVICE_CLASSES = frozenset(
    {"moisture", "smoke", "gas", "carbon_monoxide", "safety", "problem", "tamper"}
)


def safe_to_pause(
    referenced: set[str], device_classes: dict[str, str | None] | None = None
) -> tuple[bool, str]:
    """Whether an automation may be paused for the duration of a probe.

    ⛔ THIS FAILS CLOSED AND SHOULD STAY THAT WAY.
    The worst outcome of refusing to pause something is a noisier measurement.
    The worst outcome of pausing the wrong thing is a leak alert that never
    fires, an alarm that trips because the arrival automation did not disarm it,
    or a door that stays locked. Those are not comparable, so the test is
    deliberately over-broad.
    """
    device_classes = device_classes or {}
    for entity in referenced:
        domain = entity.split(".", 1)[0] if "." in entity else ""
        if domain in NEVER_PAUSE_DOMAINS:
            return False, f"touches {entity} ({domain})"
        dc = device_classes.get(entity)
        if dc in NEVER_PAUSE_DEVICE_CLASSES:
            return False, f"touches {entity} (device_class {dc})"
    return True, ""
