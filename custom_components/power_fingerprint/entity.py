"""Shared entity base.

Every entity belongs to one service device so they group in the UI instead of
scattering as loose entities. This integration measures nothing itself - it
derives everything from other integrations' sensors - so the device is marked
as a service with no physical connection.
"""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, VERSION
from .coordinator import FingerprintCoordinator


class FingerprintEntity(CoordinatorEntity[FingerprintCoordinator]):
    """Base for every Power Fingerprint entity."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: FingerprintCoordinator, key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.entry_id)},
            name="Power Fingerprint",
            manufacturer=MANUFACTURER,
            model="Load signature analysis",
            sw_version=VERSION,
            entry_type=DeviceEntryType.SERVICE,
        )
