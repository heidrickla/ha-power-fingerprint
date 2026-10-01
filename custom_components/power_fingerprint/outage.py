"""What a breaker trip did to devices that do not go unavailable when unpowered.

`breaker.TripDetector` counts an entity that goes `unavailable`, or a meter that
collapses. Three kinds of device do neither within a short outage:

- Zigbee mains devices stay available for hours. They are found by sending each
  one a command that changes nothing while the circuit is dead.
- Z-Wave JS marks a node `dead` only after a command to it fails, and leaves its
  entities available. Pinging each node makes the dead ones show.
- A TV integration may report an unreachable TV as `off`.

`OutageEvidence` collects these for one incident and judges them once power has
been back long enough for each device to answer again. Every answer here stays
`suspected`: a device that stops answering may only have lost its parent router.

A FAILURE COUNTS ONLY BESIDE A SUCCESS ON THE SAME DEVICE. A device that fails
while the circuit is dead and answers the same request after power returns lost
its path with the circuit. One that fails both times was broken anyway, and one
that answered while the circuit was dead is evidence it is fed from elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Probing starts this long after the circuit reads dead: supplies drain and
# integrations notice in the first seconds, and a breaker flicked straight back
# is over before anything is sent.
PROBE_DELAY_S = 15.0

# Devices boot and rejoin before they are asked again. Measured on Z-Wave mains
# nodes: alive again 7 to 9 s after their circuit returned.
SETTLE_S = 30.0

# How long after power returns a device may take to answer again before its
# outage evidence is judged without it. Measured: a TV reported `on` 92 s after
# its circuit returned.
RETURN_S = 120.0

# A TV that goes `off` this long after the fall was switched off, not cut off.
# Measured: 15 s after the circuit read dead.
MEDIA_OFF_S = 30.0

# The circuit's reading trails its power: the meter reports every few seconds
# and averages. Measured: Z-Wave nodes alive again 3 to 5 s before their
# circuit first read live. A device back within this of the live reading came
# back with the power; a request finished inside it says nothing either way.
METER_LAG_S = 30.0

ZIGBEE = "zigbee_identify"
# A same-state command sent by hand, recorded through the import action.
ZIGBEE_COMMAND = "zigbee_command"
ZWAVE = "zwave_node_dead"
MEDIA = "media_off"

DURING = "during"
AFTER = "after"


@dataclass(frozen=True)
class ProbeResult:
    """One request to one device: True answered, False failed, None indeterminate."""

    device: str
    phase: str
    sent: float
    done: float
    answered: bool | None


@dataclass
class _Track:
    """One device's up and down changes, from its state at the fall onward."""

    up_at_fall: bool
    stable: bool
    changes: list[tuple[float, bool, bool]] = field(default_factory=list)


@dataclass(frozen=True)
class Verdict:
    """What the outage showed, per device."""

    casualties: dict[str, dict[str, object]]
    answered: list[str]
    indeterminate: list[str]


class OutageEvidence:
    """One incident's evidence from probes, node status and TV state."""

    def __init__(self, circuit: str, fall: float) -> None:
        self.circuit = circuit
        self.fall = fall
        self.restored: float | None = None
        # Returns that did not hold: (live, dead again).
        self.flickers: list[tuple[float, float]] = []
        self._probes: list[ProbeResult] = []
        self._nodes: dict[str, _Track] = {}
        self._media: dict[str, _Track] = {}

    # --- feeding ---------------------------------------------------------

    def track_node(self, device: str, alive: bool, stable: bool) -> None:
        """A Z-Wave node's state at the fall; `stable`: held for the stable period."""
        self._nodes.setdefault(device, _Track(alive, stable))

    def track_media(self, device: str, on: bool, stable: bool) -> None:
        """A TV's state at the fall; `stable` is on for the stable period."""
        self._media.setdefault(device, _Track(on, stable))

    def node(self, device: str, t: float, alive: bool) -> None:
        if (track := self._nodes.get(device)) is not None:
            track.changes.append((t, alive, False))

    def media(self, device: str, t: float, on: bool, by_person: bool) -> None:
        """`by_person` when the change carries a user or a parent context."""
        if (track := self._media.get(device)) is not None:
            track.changes.append((t, on, by_person))

    def probe(self, result: ProbeResult) -> None:
        self._probes.append(result)

    def restore(self, t: float) -> None:
        if self.restored is None:
            self.restored = t

    def dead_again(self, t: float) -> None:
        """The circuit died again inside the same incident: its return did not hold."""
        if self.restored is not None:
            self.flickers.append((self.restored, t))
            self.restored = None

    def asked_while_dead(self) -> set[str]:
        """Devices with a request that still counts as made while dead."""
        return {
            p.device for p in self._probes if p.phase == DURING and self._while_dead(p)
        }

    # --- judging ---------------------------------------------------------

    def waiting(self, now: float) -> bool:
        """Whether a device that went down is not back yet, inside the return window."""
        if self.restored is None:
            return True
        if now >= self.restored + RETURN_S:
            return False
        for track in [*self._nodes.values(), *self._media.values()]:
            if not (track.up_at_fall and track.stable):
                continue
            down = self._down(track)
            if down is not None and self._back(track, down[0]) is None:
                return True
        return False

    def failed_zigbee(self) -> list[str]:
        """Devices whose request failed while the circuit was dead, to ask again."""
        return sorted(
            {
                p.device
                for p in self._probes
                if p.phase == DURING and p.answered is False and self._while_dead(p)
            }
        )

    def _while_dead(self, p: ProbeResult) -> bool:
        """Sent and finished inside the outage; one near a return says nothing."""
        if p.sent < self.fall:
            return False
        if self.restored is not None and p.done > self.restored - METER_LAG_S:
            return False
        # Devices powered by a return that did not hold drain again from its end.
        return all(
            p.done <= live - METER_LAG_S or p.sent >= dead + PROBE_DELAY_S
            for live, dead in self.flickers
        )

    def _after(self, p: ProbeResult) -> bool:
        """Sent after the return and finished inside its window."""
        restored = self.restored
        return (
            p.phase == AFTER
            and restored is not None
            and restored <= p.sent
            and p.done <= restored + RETURN_S
        )

    def _down(self, track: _Track) -> tuple[float, bool] | None:
        """Its first going down after the fall, and whether a person did it."""
        end = None if self.restored is None else self.restored + RETURN_S
        for t, up, by_person in track.changes:
            if t > self.fall and (end is None or t <= end) and not up:
                return t, by_person
        return None

    def _back(self, track: _Track, down: float) -> float | None:
        """When it came back with the power, after going down, within the window."""
        restored = self.restored
        assert restored is not None
        for t, up, _ in track.changes:
            if up and t > down and restored - METER_LAG_S <= t <= restored + RETURN_S:
                return t
        return None

    def judge(self) -> Verdict:
        casualties: dict[str, dict[str, object]] = {}
        answered: set[str] = set()
        indeterminate: set[str] = set()
        self._judge_zigbee(casualties, answered, indeterminate)
        self._judge_tracks(self._nodes, ZWAVE, None, casualties, indeterminate)
        self._judge_tracks(
            self._media, MEDIA, self.fall + MEDIA_OFF_S, casualties, indeterminate
        )
        indeterminate -= set(casualties) | answered
        return Verdict(
            casualties, sorted(answered - set(casualties)), sorted(indeterminate)
        )

    def _judge_zigbee(
        self,
        casualties: dict[str, dict[str, object]],
        answered: set[str],
        indeterminate: set[str],
    ) -> None:
        by_device: dict[str, list[ProbeResult]] = {}
        for p in self._probes:
            by_device.setdefault(p.device, []).append(p)
        for device, results in sorted(by_device.items()):
            during = [p for p in results if p.phase == DURING and self._while_dead(p)]
            after = [p for p in results if self._after(p)]
            failed = [p for p in during if p.answered is False]
            if any(p.answered for p in during):
                # It answered with its circuit dead: fed from elsewhere, whatever else.
                answered.add(device)
            elif failed and any(p.answered for p in after):
                casualties[device] = {
                    "signal": ZIGBEE,
                    "failed_at": failed[0].done,
                    "answered_at": next(p.done for p in after if p.answered),
                }
            else:
                indeterminate.add(device)

    def _judge_tracks(
        self,
        tracks: dict[str, _Track],
        signal: str,
        down_by: float | None,
        casualties: dict[str, dict[str, object]],
        indeterminate: set[str],
    ) -> None:
        if self.restored is None:
            return
        for device, track in sorted(tracks.items()):
            if not (track.up_at_fall and track.stable) or device in casualties:
                continue
            if (down := self._down(track)) is None:
                continue
            went, by_person = down
            back = self._back(track, went)
            late = went > self.restored or (down_by is not None and went > down_by)
            if by_person or late or back is None:
                indeterminate.add(device)
                continue
            casualties[device] = {"signal": signal, "down_at": went, "back_at": back}


def merge_outage(old: dict[str, Any] | None, new: dict[str, Any]) -> dict[str, Any]:
    """A trip's outage evidence after another write: later rows win per device.

    Each device sits in one place, in the order `judge` ranks them: a casualty,
    then answered, then recovered, then indeterminate.
    """
    old = old or {}
    casualties = {**old.get("casualties", {}), **new.get("casualties", {})}
    answered = {*old.get("answered", []), *new.get("answered", [])} - set(casualties)
    recovered = {
        k: v
        for k, v in {**old.get("recovered", {}), **new.get("recovered", {})}.items()
        if k not in casualties and k not in answered
    }
    indeterminate = {*old.get("indeterminate", []), *new.get("indeterminate", [])}
    indeterminate -= {*casualties, *answered, *recovered}
    return {
        "casualties": casualties,
        "answered": sorted(answered),
        "indeterminate": sorted(indeterminate),
        "recovered": recovered,
    }
