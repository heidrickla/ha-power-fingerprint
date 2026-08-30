"""Power Fingerprint - appliance identification from circuit load shape."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from . import services
from .const import DOMAIN
from .coordinator import FingerprintCoordinator
from .store import FingerprintStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    options = {**entry.data, **entry.options}

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

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        "store": store,
    }
    await services.async_register(hass, entry.entry_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        # Services are registered once and shared; only tear them down when the
        # last entry goes, or unloading one entry would break the others.
        if not hass.data[DOMAIN]:
            services.async_unregister(hass)
    return unloaded


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
