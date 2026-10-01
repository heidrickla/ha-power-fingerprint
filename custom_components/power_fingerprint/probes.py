"""Requests that change nothing, sent while a breaker is off, and one incident's run.

Zigbee: Identify with a time of zero to an endpoint that has the Identify
cluster, awaited. zha 2.1 raises zigpy's `DeliveryError` when the device does
not answer, and `ZHAException` when it answers with a failure status.

Z-Wave: the node's ping button. The press returns at once; Z-Wave JS marks a
node that does not answer `dead`, which its node status sensor reports.

Battery devices and anything with a lock, cover, valve or alarm panel entity
are never sent a request.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .breaker import STABLE_S
from .const import DOMAIN
from .outage import (
    AFTER,
    DURING,
    PROBE_DELAY_S,
    RETURN_S,
    SETTLE_S,
    OutageEvidence,
    ProbeResult,
)
from .verify import is_battery_powered

_LOGGER = logging.getLogger(__name__)

_SKIP_DOMAINS = frozenset({"lock", "cover", "valve", "alarm_control_panel"})
MEDIA_DOMAINS = frozenset({"media_player", "remote"})
_MEDIA_DOWN = frozenset({"off", "standby"})
_UNREAD = frozenset({STATE_UNAVAILABLE, STATE_UNKNOWN, ""})

# Exception names, so neither zha nor zigpy is imported.
_UNDELIVERED = frozenset({"DeliveryError"})
_ANSWERED_WITH_STATUS = "ZHAException"

# A Zigbee request that has not finished by now is abandoned as indeterminate.
# Measured: answers in 37 ms to 6.8 s across 41 mains devices.
REQUEST_TIMEOUT_S = 30.0
# Between Z-Wave ping presses, so the controller's queue holds one at a time.
PING_GAP_S = 1.0
# A breaker left off longer than this is not followed to its return.
MAX_OUTAGE_S = 6 * 3600.0
# A trip is decided at most GRACE_S after its circuit returns; a run with no
# decision this long after RETURN_S has lost its incident.
DECIDE_S = 300.0


@dataclass(frozen=True)
class Targets:
    """Per device: what to ask, and what to watch."""

    zigbee: dict[str, tuple[str, int]] = field(default_factory=dict)
    zwave: dict[str, tuple[str, str]] = field(default_factory=dict)
    media: dict[str, list[str]] = field(default_factory=dict)


def discover(hass: HomeAssistant, excluded: set[str]) -> Targets:
    """Mains Zigbee endpoints with Identify, mains Z-Wave nodes, TVs; by device id."""
    registry = er.async_get(hass)
    rows_by_device: dict[str, list[er.RegistryEntry]] = {}
    for row in registry.entities.values():
        if row.device_id and row.device_id not in excluded:
            rows_by_device.setdefault(row.device_id, []).append(row)
    targets = Targets()
    for device, every in sorted(rows_by_device.items()):
        # What a device is comes from all its entities; only enabled ones are used.
        rows = [r for r in every if r.disabled_by is None]
        media = sorted(r.entity_id for r in rows if r.domain in MEDIA_DOMAINS)
        if media:
            targets.media[device] = media
        classes = {
            r.entity_id: r.device_class or r.original_device_class for r in every
        }
        if is_battery_powered(classes) or {r.domain for r in every} & _SKIP_DOMAINS:
            continue
        endpoints: list[tuple[int, str]] = []
        ping = status = None
        for row in rows:
            if row.platform == "zha" and row.domain == "button":
                parts = row.unique_id.rsplit("-", 2)
                if len(parts) == 3 and parts[2] == "3" and parts[1].isdigit():
                    endpoints.append((int(parts[1]), parts[0]))
            elif row.platform == "zwave_js":
                if row.unique_id.endswith(".ping"):
                    ping = row.entity_id
                elif row.unique_id.endswith(".node_status"):
                    status = row.entity_id
        if endpoints:
            endpoint, ieee = min(endpoints)
            targets.zigbee[device] = (ieee, endpoint)
        if ping and status:
            targets.zwave[device] = (ping, status)
    return targets


def answered(err: BaseException) -> bool | None:
    """What a failed request says: False undelivered, True answered, None unknown."""
    chain: list[BaseException] = []
    seen: BaseException | None = err
    while seen is not None and seen not in chain:
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__
    names = {type(e).__name__ for e in chain}
    if names & _UNDELIVERED:
        return False
    if _ANSWERED_WITH_STATUS in names:
        return True
    return None


async def identify(hass: HomeAssistant, ieee: str, endpoint: int) -> bool | None:
    """Identify(0): True answered, False not delivered, None indeterminate."""
    try:
        async with asyncio.timeout(REQUEST_TIMEOUT_S):
            await hass.services.async_call(
                "zha",
                "issue_zigbee_cluster_command",
                {
                    "ieee": ieee,
                    "endpoint_id": endpoint,
                    "cluster_id": 3,
                    "cluster_type": "in",
                    "command": 0,
                    "command_type": "server",
                    "params": {"identify_time": 0},
                },
                blocking=True,
            )
    except TimeoutError:
        return None
    except Exception as err:
        return answered(err)
    return True


def _up_media(state: State | None) -> bool | None:
    if state is None or state.state in _UNREAD:
        return None
    return state.state not in _MEDIA_DOWN


def _by_person(state: State) -> bool:
    context = state.context
    return bool(context.user_id or context.parent_id)


class OutageRun:
    """One incident's probes and passive signals, until power is back long enough.

    Driven by Home Assistant timers. Nothing is written here: once power has
    been back `RETURN_S`, or every device that went down is back, and the trip
    was accepted, `on_done` gets the evidence. A refused trip, a circuit dead
    for `MAX_OUTAGE_S` or an unload ends the run with nothing recorded. A return
    that does not hold before the incident closes puts the run back to dead.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        circuit: str,
        fall: float,
        targets: Targets,
        probe: bool,
        on_done: Callable[[OutageRun], Coroutine[Any, Any, None]],
    ) -> None:
        self.hass = hass
        self._entry = entry
        self.circuit = circuit
        self.fall = fall
        self.targets = targets
        self.probe = probe
        self.evidence = OutageEvidence(circuit, fall)
        self.trip: dict[str, Any] | None = None
        self.decided = False
        self.finished = False
        self._on_done = on_done
        self._after_done = not probe
        # The dead-circuit deadline, the current return's timers, and the rest.
        self._dead: CALLBACK_TYPE | None = None
        self._returning: list[CALLBACK_TYPE] = []
        self._timers: list[CALLBACK_TYPE] = []
        self._tasks: set[asyncio.Task[None]] = set()
        self._probing: asyncio.Task[None] | None = None
        self._control: asyncio.Task[None] | None = None
        # Entity -> (kind, device) for the passive signals.
        self.watching: dict[str, tuple[str, str]] = {}
        # Media entity -> whether it reads up (None: unreadable); each TV's last
        # known state.
        self._media_up: dict[str, bool | None] = {}
        self._tv_last: dict[str, bool] = {}

    @callback
    def start(self) -> None:
        self._snapshot()
        self._timers.append(self._at(self.fall + PROBE_DELAY_S, self._on_probe))
        self._dead = self._at(self.fall + MAX_OUTAGE_S, self._on_expired)

    @callback
    def stop(self) -> None:
        self.finished = True
        for cancel in [*self._timers, *self._returning]:
            cancel()
        if self._dead is not None:
            self._dead()
        self._timers.clear()
        self._returning.clear()
        self._dead = None
        for task in self._tasks:
            task.cancel()

    def _snapshot(self) -> None:
        """Each device's state at the fall, and whether it held for STABLE_S."""
        for device, (_ping, status) in self.targets.zwave.items():
            state = self.hass.states.get(status)
            if state is None or state.state not in ("alive", "dead"):
                continue
            alive = state.state == "alive"
            stable = state.last_changed_timestamp <= self.fall - STABLE_S
            self.evidence.track_node(device, alive, stable)
            self.watching[status] = ("node", device)
        for device, entities in self.targets.media.items():
            states = [s for e in entities if (s := self.hass.states.get(e))]
            up = [s for s in states if _up_media(s)]
            if not up:
                continue
            stable = all(s.last_changed_timestamp <= self.fall - STABLE_S for s in up)
            self.evidence.track_media(device, True, stable)
            self._tv_last[device] = True
            for state in states:
                self._media_up[state.entity_id] = _up_media(state)
            for entity in entities:
                self.watching[entity] = ("media", device)

    @callback
    def feed(self, entity: str, new: State) -> None:
        kind, device = self.watching[entity]
        stamp = new.last_changed_timestamp
        if kind == "node":
            if new.state in ("alive", "dead"):
                self.evidence.node(device, stamp, new.state == "alive")
        else:
            self._media_up[entity] = _up_media(new)
            up = self._tv_up(device)
            if up is not None and up != self._tv_last[device]:
                self._tv_last[device] = up
                self.evidence.media(device, stamp, up, _by_person(new))
        self._check()

    def _tv_up(self, device: str) -> bool | None:
        """Up while any media entity is up, down once all are down, else unknown."""
        readings = [self._media_up.get(e) for e in self.targets.media[device]]
        if True in readings:
            return True
        if all(r is False for r in readings):
            return False
        return None

    @callback
    def restore(self, stamp: float) -> None:
        if self.evidence.restored is not None or self.finished:
            return
        self.evidence.restore(stamp)
        if self._dead is not None:
            self._dead()
            self._dead = None
        if self._probing is not None:
            # Requests near a return say nothing. Forgotten as well as cancelled, so a
            # later death starts a fresh pass however long this one takes to unwind.
            self._probing.cancel()
            self._probing = None
        self._returning = [
            self._at(stamp + SETTLE_S, self._on_settled),
            self._at(stamp + RETURN_S, self._on_tick),
            self._at(stamp + RETURN_S + DECIDE_S, self._on_expired),
        ]

    @callback
    def dead_again(self, stamp: float) -> None:
        """Its circuit died again before the incident closed: power is not back."""
        if self.evidence.restored is None or self.finished:
            return
        self.evidence.dead_again(stamp)
        for cancel in self._returning:
            cancel()
        self._returning.clear()
        if self._control is not None:
            self._control.cancel()
        self._after_done = not self.probe
        self._dead = self._at(self.fall + MAX_OUTAGE_S, self._on_expired)
        self._timers.append(self._at(stamp + PROBE_DELAY_S, self._on_probe))

    @callback
    def decide(self, trip: dict[str, Any] | None) -> None:
        """The trip it belongs to, or None when the incident was refused."""
        self.trip = trip
        self.decided = True
        if trip is None:
            self.stop()
            return
        self._check()

    @callback
    def _at(self, when: float, action: Callable[[datetime], None]) -> CALLBACK_TYPE:
        return async_call_later(self.hass, max(0.0, when - _now()), action)

    @callback
    def _spawn(self, work: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = self._entry.async_create_background_task(self.hass, work, name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    @callback
    def _on_probe(self, _now: datetime) -> None:
        if not self.probe or self.evidence.restored is not None:
            return
        if self._probing is None or self._probing.done():
            self._probing = self._spawn(self._probe_during(), f"{DOMAIN} outage probe")

    @callback
    def _on_settled(self, _now: datetime) -> None:
        if self.probe:
            self._control = self._spawn(self._probe_after(), f"{DOMAIN} outage control")

    @callback
    def _on_tick(self, _now: datetime) -> None:
        # The return window is over: a control pass still asking is cut off.
        if self._control is not None:
            self._control.cancel()
        self._after_done = True
        self._check()

    @callback
    def _on_expired(self, _now: datetime) -> None:
        if not self.finished:
            _LOGGER.info(
                "Outage evidence for %s is dropped: %s",
                self.circuit,
                "no decision on its trip"
                if self.evidence.restored is not None
                else "the circuit stayed dead",
            )
            self.stop()

    @callback
    def _check(self) -> None:
        """Hand the evidence over once everything it waits for has happened."""
        restored = self.evidence.restored
        if self.finished or restored is None or not self._after_done:
            return
        now = _now()
        if now < restored + RETURN_S and self.evidence.waiting(now):
            return
        if not self.decided or self.trip is None:
            return
        self.stop()
        self._entry.async_create_background_task(
            self.hass, self._on_done(self), f"{DOMAIN} outage record"
        )

    async def _probe_during(self) -> None:
        asked = self.evidence.asked_while_dead()
        await asyncio.gather(
            self._zigbee(DURING, sorted(set(self.targets.zigbee) - asked)),
            self._zwave(sorted(self.targets.zwave)),
        )

    async def _probe_after(self) -> None:
        # Cancelled when the return window ends or the circuit dies again.
        await asyncio.gather(
            self._zigbee(AFTER, self.evidence.failed_zigbee()),
            self._zwave(self._still_dead()),
        )
        self._after_done = True
        self._check()

    async def _zigbee(self, phase: str, devices: list[str]) -> None:
        """One request at a time: a busy mesh fails requests of its own."""
        for device in devices:
            if phase == DURING and self.evidence.restored is not None:
                return
            ieee, endpoint = self.targets.zigbee[device]
            sent = _now()
            result = await identify(self.hass, ieee, endpoint)
            self.evidence.probe(ProbeResult(device, phase, sent, _now(), result))

    async def _zwave(self, devices: list[str]) -> None:
        for device in devices:
            if self.evidence.restored is not None and device not in self._still_dead():
                continue
            ping, _status = self.targets.zwave[device]
            try:
                await self.hass.services.async_call(
                    "button", "press", {"entity_id": ping}, blocking=True
                )
            except Exception as err:
                _LOGGER.debug("Ping %s was not sent: %s", ping, err)
            await asyncio.sleep(PING_GAP_S)

    def _still_dead(self) -> list[str]:
        out = []
        for device, (_ping, status) in sorted(self.targets.zwave.items()):
            state = self.hass.states.get(status)
            if state is not None and state.state == "dead":
                out.append(device)
        return out


def _now() -> float:
    return dt_util.utcnow().timestamp()
