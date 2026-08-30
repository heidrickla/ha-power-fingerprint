"""Power Fingerprint - appliance identification from circuit load shape."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.typing import ConfigType

from . import services
from .const import CONF_CIRCUITS, CONF_MAINS, DOMAIN
from .coordinator import (
    FingerprintCoordinator,
    PowerFingerprintConfigEntry,
    PowerFingerprintData,
)
from .store import FingerprintStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the service actions.

    ⛔ REGISTERED HERE, NOT IN `async_setup_entry`. Actions registered per
    entry vanish while the entry is unloaded, and every automation that calls
    one then fails validation with "action not found" - which reads as a typo
    in the automation rather than as an integration that is temporarily down.
    Registered at component setup they always exist, and each one raises a
    translated error explaining that the integration is not loaded.
    """
    services.async_setup_services(hass)
    return True


async def async_setup_entry(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> bool:
    options: dict[str, Any] = {**entry.data, **entry.options}

    # ⛔ CHECK THE SOURCES BEFORE CLAIMING SETUP SUCCEEDED. This integration
    # derives everything from other integrations' sensors, and those can load
    # after this one. Setting up anyway produces a device full of entities
    # reading `unknown` with no explanation; raising ConfigEntryNotReady makes
    # Home Assistant retry with backoff, which is what actually fixes it.
    missing = [
        entity
        for entity in (options[CONF_MAINS], *options[CONF_CIRCUITS])
        if hass.states.get(entity) is None
    ]
    if missing:
        raise ConfigEntryNotReady(
            f"Waiting for {len(missing)} power sensor(s) to appear, "
            f"starting with {missing[0]}"
        )

    _prune_stray_devices(hass, entry)

    store = FingerprintStore(hass, entry.entry_id)
    await store.async_load()

    # ⛔ A PROBE THAT DIED MID-RUN LEAVES AUTOMATIONS SWITCHED OFF AND SILENT.
    # Nothing else would ever turn them back on, and the house would simply
    # stop reacting to things with no error anywhere. Restoring at setup is the
    # backstop for a killed process, a Home Assistant restart, or a power cut
    # during a probe. It is deliberately noisy: a warning, because this should
    # never happen quietly.
    orphaned = await store.async_take_orphaned_pauses()
    for entity in orphaned:
        _LOGGER.warning(
            "Re-enabling %s - it was switched off for a power fingerprint probe "
            "that did not finish",
            entity,
        )
        await hass.services.async_call(
            "automation", "turn_on", {"entity_id": entity}, blocking=False
        )

    coordinator = FingerprintCoordinator(hass, options, entry.entry_id, store)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PowerFingerprintData(coordinator=coordinator, store=store)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


@callback
def _prune_stray_devices(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> None:
    """Remove device entries an earlier version created by mistake.

    ⛔ Up to 0.10.0 this integration attached its per-device entities by
    returning the TARGET device's identifiers in `DeviceInfo`. That merged
    devices for years and no longer does: on Home Assistant 2026.8 it produced
    a second, nameless device entry per mapping, owned by this integration and
    sitting beside the real one. Nine of them on the development install.

    The fix is in `AttachedEntity`, but a fix that only stops the bleeding
    leaves the mess behind on every install that already ran the old version.
    Only entries this integration owns, carrying no name and not its own
    service device, are removed - the real devices are owned by whoever created
    them and are never touched.
    """
    devices = dr.async_get(hass)
    for device in list(dr.async_entries_for_config_entry(devices, entry.entry_id)):
        if device.name is not None:
            continue
        if any(domain == DOMAIN for domain, _ in device.identifiers):
            continue
        _LOGGER.info(
            "Removing a stray device entry left by an earlier version: %s",
            device.identifiers,
        )
        devices.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def async_unload_entry(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> bool:
    # The services are deliberately left registered - see `async_setup`. They
    # are component-level, not entry-level, and they refuse politely while
    # nothing is loaded.
    unloaded: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unloaded


async def _async_reload(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
