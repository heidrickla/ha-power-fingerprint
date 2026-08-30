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

import voluptuous as vol
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .analysis import segment
from .const import DOMAIN
from .fingerprint import cluster, normalize, summarize
from .verify import (
    agree,
    decide,
    grade,
    interference,
    may_probe,
    rank_deltas,
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
    }
)

LABEL_SCHEMA = vol.Schema(
    {
        vol.Required("circuit"): cv.entity_id,
        vol.Required("current_label"): cv.string,
        vol.Required("new_label"): cv.string,
    }
)


async def _history(hass: HomeAssistant, entity: str, days: int):
    """Pull one entity's history through the recorder's own API.

    Note this is NOT the REST history endpoint, which returns only ~24 hours
    from the start time unless an explicit end_time is passed. The internal API
    takes both bounds directly and has no such trap.
    """
    from homeassistant.components.recorder import get_instance, history

    start = dt_util.utcnow() - timedelta(days=days)
    end = dt_util.utcnow()

    def _fetch():
        return history.state_changes_during_period(
            hass, start, end, entity_id=entity, no_attributes=True
        )

    data = await get_instance(hass).async_add_executor_job(_fetch)
    out = []
    for state in data.get(entity, []):
        try:
            out.append((state.last_changed, float(state.state)))
        except (TypeError, ValueError):
            continue  # unavailable/unknown - a fabricated zero would lie
    return out


async def _sample_circuits(
    hass: HomeAssistant, circuits: list[str], seconds: int
) -> dict[str, float]:
    """Average each circuit over a window rather than taking one reading.

    An Emporia reports about every 12 s, so a single snapshot can be up to a
    full reporting interval stale and will disagree with itself between the
    baseline and the active read. Averaging over the settle window removes that
    and most of the noise from other loads.
    """
    samples: dict[str, list[float]] = {c: [] for c in circuits}
    ticks = max(3, seconds // 5)
    for _ in range(ticks):
        for c in circuits:
            state = hass.states.get(c)
            if state is None:
                continue
            try:
                samples[c].append(float(state.state))
            except (TypeError, ValueError):
                continue
        await asyncio.sleep(seconds / ticks)
    return {c: sum(v) / len(v) for c, v in samples.items() if v}


def _read(hass: HomeAssistant, entity: str) -> float | None:
    state = hass.states.get(entity)
    if state is None:
        return None
    try:
        return float(state.state)
    except (TypeError, ValueError):
        return None


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


async def async_register(hass: HomeAssistant, entry_id: str) -> None:
    """Register the services once, on the first config entry."""

    async def _learn(call: ServiceCall) -> ServiceResponse:
        entry_data = hass.data[DOMAIN][entry_id]
        coordinator = entry_data["coordinator"]
        store = entry_data["store"]

        circuits = call.data.get("circuits") or coordinator.circuits
        days = call.data["days"]
        threshold = call.data["threshold"]

        report: dict[str, list[dict]] = {}
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
        store = hass.data[DOMAIN][entry_id]["store"]
        ok = await store.async_relabel(
            call.data["circuit"], call.data["current_label"], call.data["new_label"]
        )
        if not ok:
            raise ServiceValidationError(
                f"No fingerprint {call.data['current_label']!r} on "
                f"{call.data['circuit']}. Run the learn service first."
            )
        await hass.data[DOMAIN][entry_id]["coordinator"].async_request_refresh()

    async def _verify(call: ServiceCall) -> ServiceResponse:
        coordinator = hass.data[DOMAIN][entry_id]["coordinator"]
        device = call.data["device"]
        meter = call.data.get("power_sensor")
        probes = call.data["probes"]
        settle = call.data["settle"]

        allowed, why = may_probe(device)
        if not allowed:
            raise ServiceValidationError(why)
        state = hass.states.get(device)
        if state is None or state.state in ("unavailable", "unknown"):
            raise ServiceValidationError(f"{device} is not available to probe")

        prior = state.state
        domain = device.split(".", 1)[0]
        observations: list[str | None] = []
        detail: list[dict] = []

        # Watch every automation that touches the device OR any circuit, so
        # interference is detected rather than inferred.
        watched_before = _watchers(hass, [device, *coordinator.circuits])

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
            "device_metered": device_metered,
            "probes": detail,
        }

    if not hass.services.has_service(DOMAIN, SERVICE_LEARN):
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


def async_unregister(hass: HomeAssistant) -> None:
    for service in (SERVICE_LEARN, SERVICE_LABEL, SERVICE_VERIFY):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
