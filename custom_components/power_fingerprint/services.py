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
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .analysis import Sample, cadences_by_cluster, sample_interval, segment, to_watts
from .attribution import assign, resample
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
    safe_to_switch_off,
)

_LOGGER = logging.getLogger(__name__)

SERVICE_LEARN = "learn"
SERVICE_LABEL = "label"
SERVICE_VERIFY = "verify_circuit"
SERVICE_MAP = "map_devices"

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
        vol.Optional("force", default=False): cv.boolean,
    }
)

MAP_SCHEMA = vol.Schema(
    {
        vol.Optional("days", default=3): vol.All(int, vol.Range(min=1, max=14)),
        vol.Optional("step"): vol.All(int, vol.Range(min=1, max=300)),
        vol.Optional("min_correlation", default=0.5): vol.All(
            vol.Coerce(float), vol.Range(min=0.0, max=1.0)
        ),
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


def _own_meter(hass: HomeAssistant, entity_id: str) -> str | None:
    """The device's own power sensor, if it has one.

    ⭐ THE SINGLE BIGGEST ACCURACY LEVER IN ACTIVE PROBING, AND ASKING THE USER
    TO SUPPLY IT WAS A FOOTGUN. With the device's own reading, `rank_deltas`
    requires the circuit's step to MATCH THE MAGNITUDE the device reported.
    Without it, ranking falls back to "which circuit moved most", and on a
    house with air conditioning the answer is always the air conditioning.

    Measured: seven disputed placements re-probed three times each with no
    meter attached. Six collapsed to `unknown` with the probes disagreeing, and
    the same two AC circuits appeared in nearly every disagreement - not
    because those lights are on them, but because a cycling 3 kW compressor
    out-moves a 10 W lamp on every axis that does not check magnitude.

    Picks the smallest-numbered candidate deterministically so repeat probes of
    the same device use the same meter and are actually comparable.
    """
    candidates = [
        entity
        for entity, device_class in _device_siblings(hass, entity_id).items()
        if device_class == "power" and hass.states.get(entity) is not None
    ]
    return sorted(candidates)[0] if candidates else None


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
            # Each shape's own rhythm, learned from when its runs actually
            # happened. This is what absence detection judges against later.
            rhythms = cadences_by_cluster(labels, [e.start for e in events])
            fps = summarize(entity, rows, labels, rhythms)
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
        # Fall back to the device's own power sensor when one was not given.
        # See _own_meter - this is what lets the ranking check magnitude rather
        # than just direction, and it is the difference between an answer and
        # the air conditioner.
        meter = call.data.get("power_sensor") or _own_meter(hass, device)
        probes = call.data["probes"]
        settle = call.data["settle"]

        # The entity's category comes from the registry, not the state - a
        # config switch looks exactly like a load switch in the state machine.
        registry = er.async_get(hass)
        entry_row = registry.async_get(device)
        category = entry_row.entity_category if entry_row else None
        allowed, why = may_probe(device, category.value if category else None)
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
        # ⛔ Refuse to cut a live load. See safe_to_switch_off - the domain
        # allowlist cannot tell a lamp from a computer's power feed, and this
        # install's living room contains both.
        ok, why_not = safe_to_switch_off(prior, _read(hass, meter) if meter else None)
        if not ok and not call.data["force"]:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="carrying_load",
                translation_placeholders={"device": device, "reason": why_not},
            )
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
        # Record it so the device's own page shows the answer, and so a mapping
        # that cost an active probe survives a restart. Only a real circuit is
        # written - see async_record_assignment; "I could not tell this time"
        # must not erase a previous answer.
        if verdict:
            await store.async_record_assignment(
                device,
                verdict,
                confidence,
                "probe",
                {
                    "probes": len(detail),
                    "agreed": answered,
                    "device_metered": device_metered,
                    "automations_fired": fired,
                },
            )
            await coordinator.async_request_refresh()

        return {
            "device": device,
            "power_sensor": meter,
            "power_sensor_source": (
                "given"
                if call.data.get("power_sensor")
                else (
                    "discovered" if meter else "none - ranking cannot check magnitude"
                )
            ),
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

    async def _map(call: ServiceCall) -> ServiceResponse:
        """Work out which circuit each self-metering device sits on.

        ⭐ THIS WAS TOOL-ONLY UNTIL NOW. The analysis lived in
        `tools/attribute.py` and needed a workstation, a token and a shell,
        which meant the one thing that turns a wall of numbered circuits into
        named ones could not be run by the person who installed the
        integration. It is the same code path; only the entry point is new.
        """
        runtime = _runtime(hass)
        coordinator = runtime.coordinator
        days = call.data["days"]
        circuits = list(coordinator.circuits)

        start = dt_util.utcnow() - timedelta(days=days)
        end = dt_util.utcnow()

        raw_circuits: dict[str, list[Sample]] = {}
        for entity in circuits:
            raw_circuits[entity] = await _history(hass, entity, days)

        # ⛔ The grid must be about one reporting interval wide, and that is a
        # property of the meter, not a constant. Measured from this install's
        # own history unless the caller overrides it.
        measured = [
            v for v in (sample_interval(rows) for rows in raw_circuits.values()) if v
        ]
        measured.sort()
        step = call.data.get("step") or (
            max(1, round(measured[len(measured) // 2])) if measured else 12
        )

        ctraces = {
            c: resample(rows, step, start, end) for c, rows in raw_circuits.items()
        }
        filled = sum(1 for t in ctraces.values() if t)
        if not filled:
            # Empty history is UNREAD, not "no matches". Say which.
            raise HomeAssistantError(
                f"The recorder returned no history for any of the {len(circuits)} "
                "circuits, so nothing could be compared. This is an unread "
                "result, not an absence of matches."
            )

        # Every power sensor that is not one of the circuits and not the mains.
        known = {*circuits, coordinator.mains}
        devices: dict[str, list[float]] = {}
        for state in hass.states.async_all("sensor"):
            if state.entity_id in known:
                continue
            attrs = state.attributes
            if attrs.get("device_class") != "power":
                continue
            if attrs.get("state_class") != "measurement":
                continue
            trace = resample(
                await _history(hass, state.entity_id, days), step, start, end
            )
            if trace:
                devices[state.entity_id] = trace

        result = assign(devices, ctraces, min_r=call.data["min_correlation"])

        # Areas are REPORTED, NEVER SCORED. Circuit identity is what this is
        # trying to learn, so feeding Home Assistant's own area names back into
        # the scoring would be circular. They are here to make a wrong answer
        # visible, which is exactly how an over-wide grid was caught.
        registry = er.async_get(hass)
        areas = ar.async_get(hass)

        def _area(entity_id: str) -> str | None:
            row = registry.async_get(entity_id)
            if row is None or row.area_id is None:
                return None
            area = areas.async_get_area(row.area_id)
            return area.name if area else None

        placed, unplaced, quiet = [], [], []
        for name, info in result.items():
            row = {
                "device": name,
                "area": _area(name),
                "circuit": info.get("circuit"),
                "step_match": info.get("step_match"),
                "correlation": info.get("r"),
                "containment": info.get("containment"),
                "margin": info.get("margin"),
                "reason": info.get("reason"),
            }
            if info.get("circuit"):
                placed.append(row)
            elif str(info.get("reason", "")) == "no transition in window":
                quiet.append(row)
            else:
                unplaced.append(row)

        # A circuit claiming devices from three unrelated areas is a smell, and
        # printing it is the only reason the over-wide grid was ever noticed.
        per_circuit: dict[str, set[str]] = {}
        for row in placed:
            area_name = str(row["area"]) if row["area"] else "?"
            per_circuit.setdefault(str(row["circuit"]), set()).add(area_name)
        suspect = sorted(c for c, a in per_circuit.items() if len(a) >= 3)

        # Passive results are recorded too, but never over a probe's answer.
        wrote = 0
        for row in placed:
            if await runtime.store.async_record_assignment(
                str(row["device"]),
                str(row["circuit"]),
                "measured" if float(row["step_match"] or 0) >= 0.6 else "inferred",
                "correlation",
                {
                    "step_match": row["step_match"],
                    "correlation": row["correlation"],
                    "margin": row["margin"],
                    "grid_seconds": step,
                },
            ):
                wrote += 1
        if wrote:
            await coordinator.async_request_refresh()

        return {
            "assignments_recorded": wrote,
            "grid_seconds": step,
            "grid_source": "given" if call.data.get("step") else "measured",
            "days": days,
            "circuits_with_history": filled,
            "devices_examined": len(devices),
            "placed": sorted(placed, key=lambda r: -float(r["step_match"] or 0)),
            "unplaced": sorted(unplaced, key=lambda r: str(r["device"])),
            "no_transition_in_window": sorted(quiet, key=lambda r: str(r["device"])),
            "circuits_claiming_three_or_more_areas": suspect,
        }

    hass.services.async_register(DOMAIN, SERVICE_LABEL, _label, schema=LABEL_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_MAP,
        _map,
        schema=MAP_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VERIFY,
        _verify,
        schema=VERIFY_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
