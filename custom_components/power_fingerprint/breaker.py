"""Breaker walk: kill a circuit and see what dies. The only causal test here.

⭐ EVERYTHING ELSE IN THIS INTEGRATION IS CIRCUMSTANTIAL. Correlation says two
things moved together. An active probe says a circuit moved when a device was
switched. Only cutting the breaker proves a device is fed by that circuit,
because the device stops.

⛔ AND IT IS THE ONE OPERATION THAT CAN COST SOMETHING. Killing a breaker kills
whatever is on it: a desktop mid-write, a rack, a freezer, a sump pump during a
storm. The app cannot flip breakers and must never pretend the choice is
routine - what it can do is say what it already believes is on a circuit BEFORE
anyone touches it, and refuse to nominate circuits carrying loads that should
not be interrupted.

⭐ THE CIRCUIT'S OWN CLAMP IS THE SELF-CHECK. If no CT drops to zero, the
breaker that was flipped is not one this integration is watching, and the right
answer is "I could not see that" rather than a mapping built from noise. This is
the same rule as everywhere else here: a blind source returns a clean-looking
empty, so make the source prove it was looking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A circuit is "dead" when it falls below this. Not zero: a CT reads a few
# hundred milliwatts of noise with nothing on it, and demanding exactly zero
# would miss every real kill.
DEAD_W = 2.0

# How far a device's own reading must fall to count as having died. Relative,
# because a 1000 W load dropping to 5 W has plainly died and a 6 W lamp
# dropping to 5 W has not.
DEAD_FRACTION = 0.1


@dataclass
class WalkResult:
    """What one breaker flip proved."""

    circuit: str | None
    circuit_before_w: float
    circuit_after_w: float
    confirmed: list[str] = field(default_factory=list)
    suspected: list[str] = field(default_factory=list)
    unaffected: list[str] = field(default_factory=list)
    reason: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "circuit": self.circuit,
            "circuit_before_w": round(self.circuit_before_w, 1),
            "circuit_after_w": round(self.circuit_after_w, 1),
            "confirmed": sorted(self.confirmed),
            "suspected": sorted(self.suspected),
            "unaffected_count": len(self.unaffected),
            "reason": self.reason,
        }


def dead_circuit(
    before: dict[str, float], after: dict[str, float], dead_w: float = DEAD_W
) -> tuple[str | None, str]:
    """Which circuit went dead, working it out rather than being told.

    A circuit qualifies only if it was drawing something beforehand and is at
    the noise floor now - a circuit that was already idle proves nothing by
    staying idle, and treating it as the answer would let a user "identify" a
    breaker they never touched.

    ⛔ MORE THAN ONE MATCH IS A REFUSAL, NOT A CHOICE. Two circuits going dead
    together means a double-pole breaker, a main, or something else switched at
    the same moment, and picking the larger would be inventing a fact.
    """
    candidates = [
        name
        for name, was in before.items()
        if was > dead_w and after.get(name, was) <= dead_w
    ]
    if not candidates:
        return None, (
            "no monitored circuit went dead - either the breaker that was "
            "flipped has no clamp on it, or it was already idle"
        )
    if len(candidates) > 1:
        names = ", ".join(sorted(candidates))
        return None, (
            f"{len(candidates)} circuits went dead together ({names}) - a "
            "double-pole breaker or a main, so which one fed what is not decidable"
        )
    return candidates[0], "the circuit's own clamp confirms it went dead"


def classify_devices(
    before: dict[str, float | None],
    after: dict[str, float | None],
    mesh_routers: frozenset[str] = frozenset(),
    dead_fraction: float = DEAD_FRACTION,
) -> tuple[list[str], list[str], list[str]]:
    """Sort devices into confirmed dead, suspected, and unaffected.

    ⛔ "WENT UNAVAILABLE" IS WEAKER EVIDENCE THAN "READS ZERO", AND CONFLATING
    THEM WOULD BE THIS PROJECT'S FAVOURITE MISTAKE. A device reporting 0 W was
    measured. A device that merely vanished might be on the circuit, or might
    be a Zigbee or Z-Wave node whose PARENT was on the circuit - kill one mains
    powered router and a dozen unrelated battery sensors go quiet with it.

    So: a measured collapse is `confirmed`, a disappearance is `suspected`, and
    anything known to route for others stays `suspected` however it looks.
    """
    confirmed: list[str] = []
    suspected: list[str] = []
    unaffected: list[str] = []

    for name, was in before.items():
        now = after.get(name)
        if was is None:
            # Nothing to compare against; its disappearance means little.
            if now is None:
                suspected.append(name)
            else:
                unaffected.append(name)
            continue
        if now is None:
            suspected.append(name)
            continue
        if was <= 0.0:
            unaffected.append(name)
            continue
        if now <= was * dead_fraction:
            (suspected if name in mesh_routers else confirmed).append(name)
        else:
            unaffected.append(name)
    return confirmed, suspected, unaffected


def walk(
    circuits_before: dict[str, float],
    circuits_after: dict[str, float],
    devices_before: dict[str, float | None],
    devices_after: dict[str, float | None],
    mesh_routers: frozenset[str] = frozenset(),
) -> WalkResult:
    """One breaker flip, start to finish."""
    circuit, reason = dead_circuit(circuits_before, circuits_after)
    confirmed, suspected, unaffected = classify_devices(
        devices_before, devices_after, mesh_routers
    )
    if circuit is None:
        # ⛔ Without a confirmed circuit there is nothing to attribute the
        # casualties TO. Report them, attribute none of them.
        return WalkResult(
            circuit=None,
            circuit_before_w=0.0,
            circuit_after_w=0.0,
            confirmed=[],
            suspected=sorted(confirmed + suspected),
            unaffected=unaffected,
            reason=reason,
        )
    return WalkResult(
        circuit=circuit,
        circuit_before_w=circuits_before.get(circuit, 0.0),
        circuit_after_w=circuits_after.get(circuit, 0.0),
        confirmed=confirmed,
        suspected=suspected,
        unaffected=unaffected,
        reason=reason,
    )


def describe(
    circuit: str,
    friendly_name: str,
    now_w: float,
    attributed: list[str],
    standby_w: float = 0.0,
    observed_hours: float = 0.0,
    ever_seen_idle: bool | None = None,
) -> dict[str, object]:
    """What is measurably true about a circuit, for someone about to flip it.

    ⛔ REPORTS, DOES NOT JUDGE. Two earlier versions of this tried to rate a
    circuit as safe or unsafe and both were wrong in opposite directions. The
    first scored names against a denylist and called anything with no match
    safe - which on a NEW install, where nothing is attributed and every
    circuit is called "Circuit 7", marked the whole panel safe including the
    one feeding a rack. The second inverted it and refused to call anything
    safe without evidence, which is merely a different way of pretending to
    know.

    ⭐ THE PERSON AT THE PANEL KNOWS THEIR OWN HOUSE. They know the freezer is
    on that wall and the desktop is running; no amount of string matching on
    "Circuit 7" competes with that. What they cannot see from the panel is what
    the meter has recorded, so that is what this returns - the floor, the
    current draw, how long it has been watched, whether it has ever been idle,
    and which devices have been attributed to it. Facts they lack, not opinions
    they do not need.

    ⚠ `standby_w` is the one worth reading twice: a circuit that never falls
    below several hundred watts has something on it that never stops, and that
    is true from the first day of measurement without anything being named.
    """
    return {
        "circuit": circuit,
        "name": friendly_name,
        "now_w": round(now_w, 1),
        # Never falls below this. The most informative number here, and the
        # only one that works before anything has been identified.
        "standby_w": round(standby_w, 1),
        "observed_hours": round(observed_hours, 1),
        "ever_seen_idle": ever_seen_idle,
        "known_devices": sorted(attributed),
    }
