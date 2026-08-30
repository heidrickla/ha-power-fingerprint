"""Persistent fingerprint library.

Stored through Home Assistant's own `Store` helper so it lands in `.storage`
with the rest of the config, survives restarts, and is included in a Home
Assistant backup without anything extra.

Only the learned CENTROIDS are persisted, never the raw events. A week of
12-second samples across 27 circuits is on the order of a million readings; the
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
        self._loaded = False

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
        self._loaded = True
        _LOGGER.debug("Loaded %d fingerprints", len(self._fingerprints))

    async def async_save(self) -> None:
        await self._store.async_save(
            {"fingerprints": [fp.to_dict() for fp in self._fingerprints]}
        )

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
