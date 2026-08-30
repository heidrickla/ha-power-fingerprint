"""Binary sensors: CT coverage fault, and state-versus-power contradiction."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import FingerprintCoordinator, PowerFingerprintConfigEntry
from .entity import FingerprintEntity

# Nothing here talks to a device or a network service - every value is derived
# from state already in memory - so there is no external system to be gentle
# with and no reason to serialise updates.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerFingerprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    async_add_entities([CoverageFault(coordinator), Contradiction(coordinator)])


class _Base(FingerprintEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    # Both of these describe the health of the measurement rather than the
    # house, which is what the diagnostic category is for: they belong in the
    # device's diagnostic section, not on a dashboard beside the power figures.
    _attr_entity_category = EntityCategory.DIAGNOSTIC


class CoverageFault(_Base):
    """On when the circuits stop adding up to the mains.

    Deliberately checks the absolute value: a NEGATIVE remainder (circuits
    summing to more than the mains) is just as wrong as a positive one and
    usually means a CT is reversed or double-counting a leg.
    """

    _attr_translation_key = "coverage_fault"

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "coverage_fault")

    @property
    def is_on(self) -> bool | None:
        data = self.coordinator.data or {}
        cov = data.get("coverage")
        if not cov or not cov.get("mains_w"):
            return None
        # Below ~200 W the panel is essentially idle and the percentage is
        # dominated by rounding, so do not raise a fault on noise.
        if cov["mains_w"] < 200:
            return False
        off_by = abs(cov["unmonitored_w"]) / cov["mains_w"] * 100.0
        return bool(off_by > data.get("tolerance_pct", 5.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        cov = (self.coordinator.data or {}).get("coverage") or {}
        return {
            "unmonitored_w": cov.get("unmonitored_w"),
            "coverage_pct": cov.get("coverage_pct"),
            "tolerance_pct": (self.coordinator.data or {}).get("tolerance_pct"),
        }


class Contradiction(_Base):
    """On when a switch claims to be on and its circuit draws nothing.

    The state machine says one thing and the current clamp says another. The
    clamp is the one measuring reality.
    """

    _attr_translation_key = "contradiction"

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "contradiction")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("contradictions"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"detail": (self.coordinator.data or {}).get("contradictions", [])}
