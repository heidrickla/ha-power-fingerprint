"""Persistent fingerprint library.

Stored through Home Assistant's own `Store` helper so it lands in `.storage`
with the rest of the config, survives restarts, and is included in a Home
Assistant backup without anything extra.

Only the learned CENTROIDS are persisted, never the raw events. A week of
6-second samples across 27 circuits runs to several million readings; the
library that summarises it is a few kilobytes. The recorder already keeps the
raw data and is the right place for it.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .fingerprint import Fingerprint, carry_labels, is_named

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1


class FingerprintStore:
    """Load and save the learned library for one config entry."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}"
        )
        self._fingerprints: list[Fingerprint] = []
        # Automations this integration switched off for a probe. Persisted so a
        # crash or restart mid-probe cannot leave them off silently - see
        # orphaned_pauses().
        self._paused: list[str] = []
        # When each named appliance was last seen running, keyed
        # "<circuit>|<label>" -> ISO timestamp.  PERSISTED ON PURPOSE. Held
        # only in memory, every Home Assistant restart would reset absence
        # detection to "never seen", so a fridge that stopped a week ago would
        # look freshly quiet after every reboot and the one alert worth having
        # would never mature.
        self._last_seen: dict[str, str] = {}
        # When the coordinator last ran. The gap between this and startup is
        # time nobody was watching, and absence detection must not count it as
        # silence.
        self._last_poll: str | None = None
        # device entity id -> what circuit it was found on, and on what evidence.
        # Persisted so the per-device entities survive a restart: a mapping that
        # cost an active probe to establish must not evaporate on reboot.
        self._assignments: dict[str, dict[str, Any]] = {}
        self._loaded = False

    @staticmethod
    def seen_key(circuit: str, label: str) -> str:
        return f"{circuit}|{label}"

    def last_seen(self) -> dict[str, str]:
        return dict(self._last_seen)

    def last_poll(self) -> str | None:
        return self._last_poll

    @property
    def fingerprints(self) -> list[Fingerprint]:
        return list(self._fingerprints)

    def labelled(self) -> list[Fingerprint]:
        """Only the ones a human has actually named.

        An `unnamed_N` centroid is a candidate, not an identification, and must
        never drive an entity - reporting "unnamed_2 is running" is worse than
        reporting nothing.
        """
        return [fp for fp in self._fingerprints if is_named(fp)]

    def unnamed(self) -> list[Fingerprint]:
        """Candidates a human has not yet identified.

        These are the whole point of the learn step and were invisible until
        now: they lived only in .storage and in the learn service's response,
        so the one screen that could tell you what your house does showed
        nothing at all until you had already named something.
        """
        return [fp for fp in self._fingerprints if not is_named(fp)]

    def for_circuit(self, circuit: str) -> list[Fingerprint]:
        return [fp for fp in self.labelled() if fp.circuit == circuit]

    def circuits(self) -> list[str]:
        return sorted({fp.circuit for fp in self.labelled()})

    async def async_load(self) -> None:
        data = await self._store.async_load()
        if data:
            self._fingerprints = [
                Fingerprint.from_dict(d) for d in data.get("fingerprints", [])
            ]
            self._paused = list(data.get("paused_automations", []))
            self._last_seen = dict(data.get("last_seen", {}))
            self._last_poll = data.get("last_poll")
            self._assignments = dict(data.get("assignments", {}))
            # SELF-HEAL: drop any assignment for this integration's OWN
            # sensors. `unmonitored_load` is mains minus the circuits, so
            # correlating it against a circuit is circular by construction, and
            # an early version did exactly that and persisted the result.
            # Fixing the mapper stops it recurring; this clears what it wrote.
            circular = [
                k for k in self._assignments if k.startswith(f"sensor.{DOMAIN}")
            ]
            for key in circular:
                del self._assignments[key]
            if circular:
                _LOGGER.info(
                    "Dropped %d self-referential circuit assignment(s): %s",
                    len(circular),
                    ", ".join(circular),
                )
        self._loaded = True
        _LOGGER.debug("Loaded %d fingerprints", len(self._fingerprints))

    def _as_dict(self) -> dict[str, Any]:
        return {
            "fingerprints": [fp.to_dict() for fp in self._fingerprints],
            "paused_automations": self._paused,
            "last_seen": self._last_seen,
            "last_poll": self._last_poll,
            "assignments": self._assignments,
        }

    async def async_save(self) -> None:
        await self._store.async_save(self._as_dict())

    def assignments(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._assignments.items()}

    async def async_record_assignment(
        self,
        device: str,
        circuit: str | None,
        confidence: str,
        source: str,
        evidence: dict[str, Any] | None = None,
    ) -> bool:
        """Remember which circuit a device was found on.

         AN ACTIVE PROBE MUST NOT OVERWRITE ITSELF WITH A WEAKER ANSWER.
        A probe that switched a real light and watched a real circuit move is
        stronger evidence than a passive correlation, and a later passive sweep
        finding nothing must not erase it. A `None` circuit is never recorded
        over an existing assignment at all - "I could not tell this time" is
        not the same as "it is not there", and this project keeps relearning
        that distinction.

        Returns True when something actually changed, so the caller can avoid
        writing to disk on every no-op refresh.
        """
        existing = self._assignments.get(device)
        if circuit is None:
            return False
        if existing and existing.get("source") == "probe" and source != "probe":
            return False
        row = {
            "circuit": circuit,
            "confidence": confidence,
            "source": source,
            "evidence": evidence or {},
        }
        if existing == row:
            return False
        self._assignments[device] = row
        await self.async_save()
        return True

    async def async_forget_assignment(self, device: str) -> bool:
        if device not in self._assignments:
            return False
        del self._assignments[device]
        await self.async_save()
        return True

    async def async_record_seen(self, seen: dict[str, str], last_poll: str) -> None:
        """Update the last-seen times and the heartbeat, then persist debounced.

        A named appliance that is RUNNING dirties this every 30-second poll,
        so an immediate write here was over a thousand full-store writes a day
        on an SD-booted appliance. The delayed save is trailing-edge: the
        write lands once the appliance stops dirtying it, and the Store's own
        final-write listener covers shutdown. What a hard crash can lose is a
        minute of last-seen freshness against hour-scale absence limits.
        """
        self._last_seen.update(seen)
        self._last_poll = last_poll
        self._store.async_delay_save(self._as_dict, 60)

    def record_poll(self, last_poll: str) -> None:
        """Refresh the poll heartbeat alone, debounced, without touching seen.

        The heartbeat used to persist only on a sighting, so after a restart
        the whole gap since the last SIGHTING was credited as blind time -
        muting the overdue alert exactly when an appliance had died. The
        coordinator calls this on a throttle; the delay coalesces bursts.
        """
        self._last_poll = last_poll
        self._store.async_delay_save(self._as_dict, 60)

    async def async_record_paused(self, entities: list[str]) -> None:
        """Write down what is about to be switched off, BEFORE switching it off.

        Order matters. If the record is written after the pause and the process
        dies in between, the automations are off and nothing knows. Written
        first, the worst case is a stale record that restores something already
        running, which is harmless.
        """
        self._paused = list(entities)
        await self.async_save()

    async def async_clear_paused(self) -> None:
        self._paused = []
        await self.async_save()

    def orphaned_pauses(self) -> list[str]:
        """Anything still recorded as paused from a previous run.

        Read at setup. A non-empty list here means a probe did not finish -
        the process was killed, Home Assistant restarted, the host lost power -
        and these automations have been switched off ever since without anyone
        being told. Deliberately does NOT clear: the caller clears only after
        the restore calls have actually been dispatched, so a failed restore
        keeps the record for the next attempt.
        """
        return list(self._paused)

    async def async_migrate_entity_id(self, old: str, new: str) -> None:
        """Rewrite every stored reference to a renamed entity, then persist.

        Fingerprints are keyed by circuit, last-seen by "circuit|label", and
        assignments both by device entity id and by circuit value - all of
        them entity ids a user can legitimately rename on the source
        integration. Without this, a rename silently orphaned the learned
        library for that circuit.
        """
        changed = False
        for fp in self._fingerprints:
            if fp.circuit == old:
                fp.circuit = new
                changed = True
        for key in [k for k in self._last_seen if k.startswith(f"{old}|")]:
            self._last_seen[key.replace(f"{old}|", f"{new}|", 1)] = self._last_seen.pop(
                key
            )
            changed = True
        if old in self._assignments:
            self._assignments[new] = self._assignments.pop(old)
            changed = True
        for info in self._assignments.values():
            if info.get("circuit") == old:
                info["circuit"] = new
                changed = True
        if changed:
            await self.async_save()

    async def async_replace_circuit(
        self, circuit: str, fingerprints: list[Fingerprint]
    ) -> None:
        """Swap in freshly learned candidates for one circuit.

        Labels a human already applied are carried across by position, so
        re-learning does not silently discard naming work. Position is used
        because clustering is ordered largest-first and a re-run over more data
        usually preserves that order - but if it does not, the labels move and
        the user has to fix them. That is visible; silently dropping them would
        not be.
        """
        old_order = [fp for fp in self._fingerprints if fp.circuit == circuit]
        fingerprints = carry_labels(old_order, fingerprints)
        self._fingerprints = [
            fp for fp in self._fingerprints if fp.circuit != circuit
        ] + fingerprints
        _LOGGER.debug(
            "Replaced %d fingerprints on %s (%d labels carried over)",
            len(fingerprints),
            circuit,
            sum(1 for fp in fingerprints if is_named(fp)),
        )
        await self.async_save()

    async def async_relabel(self, circuit: str, old: str, new: str) -> bool:
        for fp in self._fingerprints:
            if fp.circuit == circuit and fp.label == old:
                fp.label = new
                await self.async_save()
                return True
        return False
