"""Binary sensors: CT coverage fault, and state-versus-power contradiction."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FingerprintCoordinator
from .entity import FingerprintEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FingerprintCoordinator = entry_data["coordinator"]
    async_add_entities([CoverageFault(coordinator), Contradiction(coordinator)])


class _Base(FingerprintEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.PROBLEM


class CoverageFault(_Base):
    """On when the circuits stop adding up to the mains.

    Deliberately checks the absolute value: a NEGATIVE remainder (circuits
    summing to more than the mains) is just as wrong as a positive one and
    usually means a CT is reversed or double-counting a leg.
    """

    _attr_name = "CT coverage fault"
    _attr_icon = "mdi:alert-circle-check"

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
        return off_by > data.get("tolerance_pct", 5.0)

    @property
    def extra_state_attributes(self) -> dict:
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

    _attr_name = "State contradiction"
    _attr_icon = "mdi:flash-alert"

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "contradiction")

    @property
    def is_on(self) -> bool:
        return bool((self.coordinator.data or {}).get("contradictions"))

    @property
    def extra_state_attributes(self) -> dict:
        return {"detail": (self.coordinator.data or {}).get("contradictions", [])}
