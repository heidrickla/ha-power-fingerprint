"""Shared entity base.

Every entity belongs to one service device so they group in the UI instead of
scattering as loose entities. This integration measures nothing itself - it
derives everything from other integrations' sensors - so the device is marked
as a service with no physical connection.
"""

from __future__ import annotations

from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, VERSION
from .coordinator import FingerprintCoordinator


class FingerprintEntity(CoordinatorEntity[FingerprintCoordinator]):
    """Base for every Power Fingerprint entity."""

    _attr_has_entity_name = True

    @property
    def available(self) -> bool:
        """Unavailable only when every source is gone.

        A single circuit dropping out is not an outage - it is exactly the
        condition the coverage sensor exists to report, and blanking every
        entity would hide the report along with the fault.
        """
        return bool(super().available) and self.coordinator.sources_available

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


class AttachedEntity(CoordinatorEntity[FingerprintCoordinator]):
    """An entity that lives on SOMEONE ELSE'S device.

    ⭐ THE FINGERPRINT BELONGS WHERE THE DEVICE IS. A circuit assignment listed
    on this integration's own service device is a fact filed under the wrong
    heading - you look it up when you already know to ask. On the device's own
    page it is there when you open the front porch light to see why it is
    behaving oddly.

    ⛔ DO NOT DO THIS BY RETURNING THE TARGET DEVICE'S IDENTIFIERS IN
    `DeviceInfo`. That was the documented trick for years and it no longer
    merges. Measured on Home Assistant 2026.8 against a real registry: passing
    identifiers `[["zha", "64:02:8f:ff:fe:a1:ca:4a"]]` that matched an existing
    Inovelli device EXACTLY produced a second, nameless device entry owned by
    this integration, sitting beside the real one. Nine of them, one per
    mapping, silently polluting the device registry. The registry now carries
    `composite_device_id` and `has_composite_identifiers` fields; shared
    identifiers across config entries are a composite relationship rather than
    a merge.

    The supported route is to let the entity register with no device at all and
    then point its REGISTRY ROW at the target device, which is what the helper
    integrations do. The entity stays owned by this config entry; only its
    placement in the UI changes.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: FingerprintCoordinator,
        key: str,
        target_device_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{coordinator.entry_id}_{key}"
        self._target_device_id = target_device_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # After registration, so there is a registry row to point.
        registry = er.async_get(self.hass)
        row = registry.async_get(self.entity_id)
        if row is not None and row.device_id != self._target_device_id:
            registry.async_update_entity(
                self.entity_id, device_id=self._target_device_id
            )

    @property
    def available(self) -> bool:
        return bool(super().available) and self.coordinator.sources_available
