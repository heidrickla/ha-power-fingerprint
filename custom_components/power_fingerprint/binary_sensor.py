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
    async_add_entities(
        [
            CoverageFault(coordinator),
            Contradiction(coordinator),
            SilentAppliance(coordinator),
        ]
    )


class _Base(FingerprintEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM


class _Diagnostic(_Base):
    """For checks about the health of the MEASUREMENT rather than the house.

    Coverage and contradiction both say "this integration's own view is
    suspect", which belongs in the device's diagnostic section rather than on a
    dashboard beside the power figures. ⚠ A silent appliance is NOT one of
    these - it is a fact about the house and the whole reason someone installs
    this, so it stays a primary entity.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC


class CoverageFault(_Diagnostic):
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


class Contradiction(_Diagnostic):
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


class SilentAppliance(_Base):
    """On when a named appliance has stopped running when it should have.

    ⭐ THE ONLY CHECK HERE THAT ALERTS ON TOO LITTLE. Everything else in this
    integration fires when something exceeds something; the expensive failures
    are the quiet ones. A freezer that stopped cycling crosses no threshold, a
    sump pump silent through a storm draws no current, and neither shows up in
    a dashboard of maxima. They show up as a rhythm that stopped.

    ⛔ Returns `None` - unknown - rather than `off` when the circuit was not
    observable for a meaningful part of the window. `off` would assert the
    appliance is fine; `None` says nobody was watching. Those are different
    claims and only one of them is true after a restart or a dropout.
    """

    _attr_translation_key = "silent_appliance"

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "silent_appliance")

    @property
    def is_on(self) -> bool | None:
        detail = (self.coordinator.data or {}).get("absence_detail") or []
        if not detail:
            return None
        if any(row["state"] == "overdue" for row in detail):
            return True
        if all(row["state"] == "unknown" for row in detail):
            return None
        return False

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        detail = data.get("absence_detail") or []
        return {
            "overdue": [row["appliance"] for row in data.get("absent", [])],
            "watching": len(detail),
            # Named separately from `off`: these are the ones the integration
            # is deliberately declining to judge.
            "unjudgeable": [
                row["appliance"] for row in detail if row["state"] == "unknown"
            ],
            "detail": detail,
        }
