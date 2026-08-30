"""Services: learn candidate fingerprints, and name them.

Learning reads history from the recorder rather than waiting for the rolling
window to fill, because a useful library needs days of data and nobody wants to
wait a week after installing.

The split between the two services is deliberate. `learn` proposes clusters and
describes them in plain English; `label` is how a human turns a candidate into
an identification. Clustering finds the recurring shapes and genuinely cannot
name them - naming needs someone who knows what is plugged in.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    State,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .analysis import Sample, segment, to_watts
from .const import DOMAIN
from .coordinator import (
    PowerFingerprintConfigEntry,
    PowerFingerprintData,
)
from .fingerprint import cluster, normalize, summarize
from .verify import (
    agree,
    decide,
    grade,
    interference,
    is_battery_powered,
    may_probe,
    rank_deltas,
    safe_to_pause,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_LEARN = "learn"
SERVICE_LABEL = "label"
SERVICE_VERIFY = "verify_circuit"

LEARN_SCHEMA = vol.Schema(
    {
        vol.Optional("circuits"): cv.entity_ids,
        vol.Optional("days", default=7): vol.All(int, vol.Range(min=1, max=30)),
        vol.Optional("threshold", default=0.9): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=5.0)
        ),
    }
)

VERIFY_SCHEMA = vol.Schema(
    {
        vol.Required("device"): cv.entity_id,
        vol.Optional("power_sensor"): cv.entity_id,
        vol.Optional("probes", default=2): vol.All(int, vol.Range(min=1, max=5)),
        vol.Optional("settle", default=30): vol.All(int, vol.Range(min=10, max=120)),
        vol.Optional("pause_automations", default=False): cv.boolean,
    }
)

LABEL_SCHEMA = vol.Schema(
    {
        vol.Required("circuit"): cv.entity_id,
        vol.Required("current_label"): cv.string,
        vol.Required("new_label"): cv.string,
    }
)


async def _history(hass: HomeAssistant, entity: str, days: int) -> list[Sample]:
    """Pull one entity's history through the recorder's own API.

    Note this is NOT the REST history endpoint, which returns only ~24 hours
    from the start time unless an explicit end_time is passed. The internal API
    takes both bounds directly and has no such trap.
    """
    from homeassistant.components.recorder import get_instance, history

    start = dt_util.utcnow() - timedelta(days=days)
    end = dt_util.utcnow()

    def _fetch() -> dict[str, list[State]]:
        rows: dict[str, list[State]] = history.state_changes_during_period(
            hass, start, end, entity_id=entity, no_attributes=True
        )
        return rows

    data = await get_instance(hass).async_add_executor_job(_fetch)
    # `no_attributes=True` strips the unit from every row, so it comes from
    # the live state and is applied to the whole window - the same approach
    # the coordinator takes when it seeds from the recorder.
    live = hass.states.get(entity)
    unit = live.attributes.get("unit_of_measurement") if live else None
    out = []
    for state in data.get(entity, []):
        try:
            watts = to_watts(float(state.state), unit)
        except (TypeError, ValueError):
            continue  # unavailable/unknown - a fabricated zero would lie
        if watts is not None:
            out.append((state.last_changed, watts))
    return out


async def _sample_circuits(
    hass: HomeAssistant, circuits: list[str], seconds: int
) -> dict[str, float]:
    """Average each circuit over a window rather than taking one reading.

    A meter reports on its own schedule - every ~6 s on the Emporia Vue this
    was developed against - so a single snapshot can be a full reporting
    interval stale and will disagree with itself between the baseline and the
    active read. Averaging over the settle window removes that and most of the
    noise from other loads.

    ⚠ THE SETTLE WINDOW HAS TO OUTLAST YOUR METER'S REPORTING INTERVAL. If it
    does not, the circuit has not reported since the switch was thrown and the
    probe reads a confident "no change" from stale values - a wrong answer that
    looks like a clean one. The caller warns when the measured cadence says
    this is happening; it cannot be fixed by sampling harder.
    """
    samples: dict[str, list[float]] = {c: [] for c in circuits}
    ticks = max(3, seconds // 5)
    for _ in range(ticks):
        for c in circuits:
            watts = _read(hass, c)
            if watts is not None:
                samples[c].append(watts)
        await asyncio.sleep(seconds / ticks)
    return {c: sum(v) / len(v) for c, v in samples.items() if v}


def _read(hass: HomeAssistant, entity: str) -> float | None:
    """One reading in watts. Every caller of this is comparing against a
    watt-denominated threshold, so the conversion cannot be optional: on a kW
    meter the raw numbers are a thousand times too small and every probe delta
    disappears under the noise floor as a confident "no change"."""
    state = hass.states.get(entity)
    if state is None:
        return None
    try:
        value = float(state.state)
    except (TypeError, ValueError):
        return None
    return to_watts(value, state.attributes.get("unit_of_measurement"))


def _watchers(hass: HomeAssistant, entities: list[str]) -> dict[str, str | None]:
    """Snapshot `last_triggered` for every automation referencing these entities.

    Uses Home Assistant's own `referenced_entities`, so it finds automations by
    what they actually touch rather than by anyone maintaining a list. Comparing
    the snapshot either side of a probe is the only reliable way to know whether
    an automation moved the device mid-measurement - the power trace alone
    cannot show it.
    """
    snapshot: dict[str, str | None] = {}
    component = hass.data.get("automation")
    if component is None:
        return snapshot
    targets = set(entities)
    for auto in component.entities:
        try:
            referenced = auto.referenced_entities
        except AttributeError:  # older core, or a blueprint that cannot resolve
            continue
        if targets & set(referenced):
            state = hass.states.get(auto.entity_id)
            snapshot[auto.entity_id] = (
                state.attributes.get("last_triggered") if state else None
            )
    return snapshot


def _pausable(
    hass: HomeAssistant, entities: list[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Which watching automations may safely be switched off for a probe.

    Returns (pausable, refused-with-reasons). Refusals are returned rather than
    swallowed, because "we left three automations running" changes how much to
    trust the measurement and the caller should see it.
    """
    pausable: list[str] = []
    refused: list[dict[str, str]] = []
    component = hass.data.get("automation")
    if component is None:
        return pausable, refused

    targets = set(entities)
    for auto in component.entities:
        try:
            referenced = set(auto.referenced_entities)
        except AttributeError:
            continue
        if not (targets & referenced):
            continue
        state = hass.states.get(auto.entity_id)
        if state is None or state.state != "on":
            continue  # already off; leave it alone and do not re-enable it later
        classes = {
            e: (
                hass.states.get(e).attributes.get("device_class")
                if hass.states.get(e)
                else None
            )
            for e in referenced
        }
        ok, why = safe_to_pause(referenced, classes)
        if ok:
            pausable.append(auto.entity_id)
        else:
            refused.append({"automation": auto.entity_id, "reason": why})
    return sorted(pausable), refused


def _device_siblings(hass: HomeAssistant, entity_id: str) -> dict[str, str | None]:
    """device_class of every entity sharing a device with this one.

    Used to tell a battery device from a mains one. Returns empty if the entity
    is not in the registry or has no device, which is treated as "unknown"
    rather than "battery" - refusing to probe something on missing metadata
    would be worse than probing it.
    """
    from homeassistant.helpers import entity_registry as er

    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None or entry.device_id is None:
        return {}
    out: dict[str, str | None] = {}
    for other in er.async_entries_for_device(
        registry, entry.device_id, include_disabled_entities=True
    ):
        state = hass.states.get(other.entity_id)
        out[other.entity_id] = (
            state.attributes.get("device_class")
            if state
            else other.original_device_class
        )
    return out


def _runtime(hass: HomeAssistant) -> PowerFingerprintData:
    """The loaded entry's runtime data, or a translated refusal.

    The actions are registered at component setup and therefore exist even
    while the entry is unloaded - so each one has to say so itself. A bare
    KeyError here would surface as an unhandled exception in the automation
    trace, which tells the user nothing about what to do.
    """
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="not_loaded"
        )
    entry: PowerFingerprintConfigEntry = entries[0]
    runtime: PowerFingerprintData = entry.runtime_data
    return runtime


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions at component setup, independent of any entry."""

    async def _learn(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        coordinator = runtime.coordinator
        store = runtime.store

        circuits = call.data.get("circuits") or coordinator.circuits
        days = call.data["days"]
        threshold = call.data["threshold"]

        report: dict[str, list[dict[str, Any]]] = {}
        for entity in circuits:
            samples = await _history(hass, entity, days)
            if not samples:
                # Empty history is UNREAD, not "nothing ran" - say which.
                report[entity] = [{"error": "no history in window"}]
                continue
            events = segment(samples)
            if not events:
                report[entity] = []
                continue
            rows = [e.as_features() for e in events]
            labels = cluster(normalize(rows), threshold=threshold)
            fps = summarize(entity, rows, labels)
            await store.async_replace_circuit(entity, fps)
            report[entity] = [
                {
                    "label": fp.label,
                    "runs": fp.count,
                    "description": fp.describe(),
                }
                for fp in fps
            ]

        await coordinator.async_request_refresh()
        return {"circuits": report}

    async def _label(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        ok = await runtime.store.async_relabel(
            call.data["circuit"], call.data["current_label"], call.data["new_label"]
        )
        if not ok:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_fingerprint",
                translation_placeholders={
                    "label": str(call.data["current_label"]),
                    "circuit": str(call.data["circuit"]),
                },
            )
        await runtime.coordinator.async_request_refresh()

    async def _verify(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        coordinator = runtime.coordinator
        device = call.data["device"]
        meter = call.data.get("power_sensor")
        probes = call.data["probes"]
        settle = call.data["settle"]

        allowed, why = may_probe(device)
        if not allowed:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="may_not_probe",
                translation_placeholders={"device": device, "reason": why},
            )
        state = hass.states.get(device)
        if state is None or state.state in ("unavailable", "unknown"):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="device_unavailable",
                translation_placeholders={"device": device},
            )

        # A battery device draws no mains current, so no CT can ever see it
        # switch. Refuse up front rather than spending the full settle time
        # arriving at "no answer" - and rather than risking an unrelated load
        # that moved during those minutes being credited to it.
        if is_battery_powered(_device_siblings(hass, device)):
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="battery_powered",
                translation_placeholders={"device": device},
            )

        prior = state.state
        domain = device.split(".", 1)[0]
        observations: list[str | None] = []
        detail: list[dict[str, Any]] = []
        notes: list[str] = []

        # Watch every automation that touches the device OR any circuit, so
        # interference is detected rather than inferred.
        watching = [device, *coordinator.circuits]
        watched_before = _watchers(hass, watching)

        # ⚠ A settle window shorter than the meter's own reporting interval
        # measures nothing: the circuit has not published since the switch was
        # thrown, so both reads come from the same stale value and the probe
        # returns a confident "no change". Say so rather than returning a
        # clean-looking wrong answer. The cadence is measured from this
        # install's own history, not assumed from any particular meter.
        profile = coordinator.source_profile()
        raw_cadence = profile.get("median_report_interval_s")
        cadence = float(raw_cadence) if isinstance(raw_cadence, int | float) else None
        if cadence and settle < cadence * 2:
            warning = (
                f"settle={settle}s is short for a meter reporting every "
                f"{cadence:.0f}s - a probe needs at least two reports after "
                f"the switch. Re-run with settle={int(cadence * 2) + 1} or more."
            )
            _LOGGER.warning("%s: %s", device, warning)
            notes.append(warning)

        store = runtime.store
        paused: list[str] = []
        refused: list[dict[str, str]] = []
        if call.data["pause_automations"]:
            paused, refused = _pausable(hass, watching)
            # ⛔ RECORD BEFORE SWITCHING OFF, NOT AFTER. If the process dies
            # between the two, a stale record restores something already
            # running, which is harmless. The other order leaves automations
            # off with nothing aware of it.
            await store.async_record_paused(paused)
            for entity in paused:
                await hass.services.async_call(
                    "automation", "turn_off", {"entity_id": entity}, blocking=True
                )
            _LOGGER.info(
                "Paused %d automations for a probe of %s (%d refused as unsafe)",
                len(paused),
                device,
                len(refused),
            )

        try:
            for _ in range(probes):
                base_circuits = await _sample_circuits(
                    hass, coordinator.circuits, settle
                )
                base_device = _read(hass, meter) if meter else None

                # Toggle AWAY from whatever it is now, so a device that is
                # already on is probed by switching it off. Insisting on
                # "on" would refuse to verify anything already running.
                target = "turn_off" if prior == "on" else "turn_on"
                await hass.services.async_call(
                    domain, target, {"entity_id": device}, blocking=True
                )
                await asyncio.sleep(settle)

                active_circuits = await _sample_circuits(
                    hass, coordinator.circuits, settle
                )
                active_device = _read(hass, meter) if meter else None

                if base_device is not None and active_device is not None:
                    expected = active_device - base_device
                else:
                    # Without the device's own meter, fall back to the largest
                    # coherent circuit change. Weaker, and flagged as such.
                    expected = max(
                        (
                            active_circuits.get(c, 0.0) - base_circuits.get(c, 0.0)
                            for c in base_circuits
                        ),
                        key=abs,
                        default=0.0,
                    )

                ranked = rank_deltas(base_circuits, active_circuits, expected)
                circuit, reason = decide(ranked)
                observations.append(circuit)
                detail.append(
                    {
                        "expected_w": round(expected, 1),
                        "circuit": circuit,
                        "reason": reason,
                        "candidates": ranked[:3],
                        "device_metered": expected is not None,
                    }
                )

                await hass.services.async_call(
                    domain,
                    "turn_on" if prior == "on" else "turn_off",
                    {"entity_id": device},
                    blocking=True,
                )
                await asyncio.sleep(settle)
        finally:
            # ⛔ RESTORE IS IN `finally`, NOT ON THE HAPPY PATH. A probe that
            # leaves the house in a different state than it found it is a bug
            # regardless of what it learned, and an exception mid-probe is
            # exactly when that would otherwise happen.
            await hass.services.async_call(
                domain,
                "turn_on" if prior == "on" else "turn_off",
                {"entity_id": device},
                blocking=True,
            )
            for entity in paused:
                await hass.services.async_call(
                    "automation", "turn_on", {"entity_id": entity}, blocking=True
                )
            if paused:
                await store.async_clear_paused()

        measured, agree_reason = agree(observations)
        fired = interference(
            watched_before, _watchers(hass, [device, *coordinator.circuits])
        )
        device_metered = any(d.get("device_metered") for d in detail)
        answered = sum(1 for d in detail if d.get("circuit"))
        verdict, confidence = grade(
            measured, fired, device_metered, answered, len(detail)
        )
        return {
            "device": device,
            "circuit": verdict,
            "confidence": confidence,
            "reason": agree_reason,
            "automations_fired_during_probe": fired,
            "automations_paused": paused,
            "automations_refused_as_unsafe": refused,
            "device_metered": device_metered,
            # Anything that qualifies the result and is not a verdict. Returned
            # rather than only logged, because whoever called the service is
            # the one who needs to see it.
            "notes": notes,
            "probes": detail,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_LEARN,
        _learn,
        schema=LEARN_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, SERVICE_LABEL, _label, schema=LABEL_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_VERIFY,
        _verify,
        schema=VERIFY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
