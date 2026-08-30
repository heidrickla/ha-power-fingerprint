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

_LOGGER = logging.getLogger(__name__)

SERVICE_LEARN = "learn"
SERVICE_LABEL = "label"

LEARN_SCHEMA = vol.Schema(
    {
        vol.Optional("circuits"): cv.entity_ids,
        vol.Optional("days", default=7): vol.All(int, vol.Range(min=1, max=30)),
        vol.Optional("threshold", default=0.9): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=5.0)
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

    if not hass.services.has_service(DOMAIN, SERVICE_LEARN):
        hass.services.async_register(
            DOMAIN,
            SERVICE_LEARN,
            _learn,
            schema=LEARN_SCHEMA,
            supports_response=SupportsResponse.ONLY,
        )
        hass.services.async_register(DOMAIN, SERVICE_LABEL, _label, schema=LABEL_SCHEMA)


def async_unregister(hass: HomeAssistant) -> None:
    for service in (SERVICE_LEARN, SERVICE_LABEL):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)
