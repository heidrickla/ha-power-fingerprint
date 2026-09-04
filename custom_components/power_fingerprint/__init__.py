"""Power Fingerprint - appliance identification from circuit load shape."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_entity_registry_updated_event
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import services
from .const import CONF_CIRCUITS, CONF_MAINS, CONF_PAIRS, DOMAIN
from .coordinator import (
    FingerprintCoordinator,
    PowerFingerprintConfigEntry,
    PowerFingerprintData,
)
from .store import FingerprintStore

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]


# Nothing is configured from YAML. hassfest requires an integration that has
# an async_setup to say so explicitly.
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the service actions.

     REGISTERED HERE, NOT IN `async_setup_entry`. Actions registered per
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

    # Sources belong to other integrations and may load after this one.
    # Setting up regardless gives a device full of `unknown`; ConfigEntryNotReady
    # retries with backoff instead.
    missing = [
        entity
        for entity in (options[CONF_MAINS], *options[CONF_CIRCUITS])
        if hass.states.get(entity) is None
    ]
    if missing:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="sources_missing",
            translation_placeholders={
                "count": str(len(missing)),
                "entity": missing[0],
            },
        )

    _prune_stray_devices(hass, entry)

    store = FingerprintStore(hass, entry.entry_id)
    await store.async_load()

    # A probe killed mid-run leaves automations switched off with nothing
    # aware of it. Restoring here is the backstop for a crash, restart or power
    # cut, and warns rather than doing it quietly. The record is cleared only
    # AFTER the restores dispatch - clearing first meant one raised call lost
    # the list while the automations stayed off. The automation integration is
    # a manifest dependency, but a missing service still must not strand them:
    # ConfigEntryNotReady keeps the record and retries.
    orphaned = store.orphaned_pauses()
    if orphaned and not hass.services.has_service("automation", "turn_on"):
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="automation_service_missing",
            translation_placeholders={"count": str(len(orphaned))},
        )
    for entity in orphaned:
        _LOGGER.warning(
            "Re-enabling %s - it was switched off for a power fingerprint probe "
            "that did not finish",
            entity,
        )
        await hass.services.async_call(
            "automation", "turn_on", {"entity_id": entity}, blocking=False
        )
    if orphaned:
        await store.async_clear_paused()

    coordinator = FingerprintCoordinator(hass, options, entry, store)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = PowerFingerprintData(coordinator=coordinator, store=store)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    _track_source_renames(hass, entry, options, store)
    return True


@callback
def _track_source_renames(
    hass: HomeAssistant,
    entry: PowerFingerprintConfigEntry,
    options: dict[str, Any],
    store: FingerprintStore,
) -> None:
    """Follow the configured sources through entity_id renames.

    The circuit list, the store's keys and this integration's own unique_ids
    all embed source entity ids, and renaming a sensor on the meter
    integration is an ordinary, supported user action. Without this, a rename
    orphaned the learned library and minted duplicate entities.

    Assignments recorded after setup are picked up on the next reload, when
    the subscription is rebuilt from the store.
    """
    tracked = {options[CONF_MAINS], *options[CONF_CIRCUITS]}
    for pair in str(options.get(CONF_PAIRS, "")).split(";"):
        for part in pair.split(","):
            if part.strip():
                tracked.add(part.strip())
    tracked.update(store.assignments())

    async def _renamed(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
        data = event.data
        if data["action"] != "update" or "old_entity_id" not in data:
            return
        old, new = data["old_entity_id"], data["entity_id"]
        _LOGGER.info("Following a rename of %s to %s", old, new)
        await store.async_migrate_entity_id(old, new)

        # This integration's own registry rows key their unique_ids on the
        # source id - migrate them so history stays attached.
        registry = er.async_get(hass)
        for row in list(er.async_entries_for_config_entry(registry, entry.entry_id)):
            if old in row.unique_id:
                registry.async_update_entity(
                    row.entity_id, new_unique_id=row.unique_id.replace(old, new)
                )

        # Last: updating the entry fires the reload listener, which rebuilds
        # everything - including this subscription - from the new ids.
        new_data = dict(entry.data)
        if new_data.get(CONF_MAINS) == old:
            new_data[CONF_MAINS] = new
        new_data[CONF_CIRCUITS] = [
            new if c == old else c for c in new_data.get(CONF_CIRCUITS, [])
        ]
        if CONF_PAIRS in new_data:
            new_data[CONF_PAIRS] = str(new_data[CONF_PAIRS]).replace(old, new)
        if new_data != dict(entry.data):
            hass.config_entries.async_update_entry(entry, data=new_data)

    entry.async_on_unload(
        async_track_entity_registry_updated_event(hass, list(tracked), _renamed)
    )


@callback
def _prune_stray_devices(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> None:
    """Remove nameless device entries this integration created by mistake.

    Attaching an entity by returning another device's identifiers produces a
    duplicate device rather than merging. Only entries this integration owns,
    with no name and not its own service device, are removed.
    """
    devices = dr.async_get(hass)
    for device in list(dr.async_entries_for_config_entry(devices, entry.entry_id)):
        if device.name is not None:
            continue
        if any(domain == DOMAIN for domain, _ in device.identifiers):
            continue
        _LOGGER.info(
            "Removing a stray device entry: %s",
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
    if unloaded:
        # A clean unload stamps the heartbeat exactly, so an options-change
        # reload credits zero blind time instead of the whole debounce window.
        runtime = entry.runtime_data
        runtime.store.record_poll(dt_util.utcnow().isoformat())
        await runtime.store.async_save()
    return unloaded


async def async_remove_entry(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> None:
    """Delete the learned library with the entry.

    The store is keyed on the entry id, so a re-added entry could never find
    it again; left behind it is an orphan in .storage that the README used to
    claim was removed. Called after unload, so no coordinator holds it open.
    """
    await FingerprintStore(hass, entry.entry_id).async_remove()


async def _async_reload(
    hass: HomeAssistant, entry: PowerFingerprintConfigEntry
) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
