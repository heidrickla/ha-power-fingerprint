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
from typing import Any, cast

import voluptuous as vol
from homeassistant.components.automation import (
    automations_with_entity,
    entities_in_automation,
)
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
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import label_registry as lr
from homeassistant.util import dt as dt_util

from .analysis import Sample, cadences_by_cluster, sample_interval, segment, to_watts
from .attribution import assign, resample
from .const import DOMAIN
from .coordinator import (
    PowerFingerprintConfigEntry,
    PowerFingerprintData,
)
from .dashboard import async_circuit_names
from .fingerprint import (
    Fingerprint,
    cluster,
    is_named,
    normalize,
    suggest_label,
    summarize,
)
from .verify import (
    INFERRED,
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
SERVICE_AUTOLABEL = "autolabel"
SERVICE_LABELS = "apply_circuit_labels"

LEARN_SCHEMA = vol.Schema(
    {
        vol.Optional("circuits"): cv.entity_ids,
        vol.Optional("days", default=7): vol.All(int, vol.Range(min=1, max=30)),
        vol.Optional("threshold"): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=5.0)
        ),
    }
)

VERIFY_SCHEMA = vol.Schema(
    {
        vol.Required("device"): cv.entity_id,
        vol.Optional("power_sensor"): cv.entity_id,
        vol.Optional("probes"): vol.All(int, vol.Range(min=1, max=5)),
        vol.Optional("settle", default=30): vol.All(int, vol.Range(min=10, max=120)),
        vol.Optional("pause_automations", default=False): cv.boolean,
        vol.Optional("force", default=False): cv.boolean,
    }
)

LABELS_SCHEMA = vol.Schema(
    {
        # Default TRUE. This writes into the user's own label namespace, for
        # which no integration API is documented, so the safe direction is to
        # show what it would do and let a person ask again.
        vol.Optional("dry_run", default=True): cv.boolean,
        vol.Optional("remove", default=False): cv.boolean,
        vol.Optional("prefix", default="Circuit"): cv.string,
    }
)

MAP_SCHEMA = vol.Schema(
    {
        vol.Optional("days", default=3): vol.All(int, vol.Range(min=1, max=14)),
        vol.Optional("step"): vol.All(int, vol.Range(min=1, max=300)),
        vol.Optional("min_correlation"): vol.All(
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


async def _histories(
    hass: HomeAssistant, entities: list[str], days: int
) -> dict[str, list[Sample]]:
    """Pull several entities' history in ONE recorder query.

    Note this is NOT the REST history endpoint, which returns only ~24 hours
    from the start time unless an explicit end_time is passed. The internal API
    takes both bounds directly and has no such trap. Batched because a query
    per entity was a full recorder round-trip each - 27 of them per learn on
    the development panel - for rows one query returns together.
    """
    from homeassistant.components.recorder import history
    from homeassistant.helpers.recorder import get_instance

    start = dt_util.utcnow() - timedelta(days=days)
    end = dt_util.utcnow()

    def _fetch() -> dict[str, list[State]]:
        rows = history.get_significant_states(
            hass,
            start,
            end,
            entity_ids=list(entities),
            include_start_time_state=False,
            no_attributes=True,
        )
        # Minimal-response rows may appear beside State objects; only States
        # carry a readable value.
        return {
            entity_id: [s for s in states if isinstance(s, State)]
            for entity_id, states in rows.items()
        }

    # The recorder is an after-dependency, not a dependency, so a user who has
    # disabled it can still call these actions. get_instance then raises a
    # bare KeyError, which the frontend shows as a stack trace.
    try:
        recorder = get_instance(hass)
    except KeyError as err:
        raise HomeAssistantError(
            translation_domain=DOMAIN, translation_key="recorder_not_running"
        ) from err
    data = await recorder.async_add_executor_job(_fetch)
    # `no_attributes=True` strips the unit from every row, so it comes from
    # the live state and is applied to the whole window - the same approach
    # the coordinator takes when it seeds from the recorder.
    out: dict[str, list[Sample]] = {}
    for entity in entities:
        live = hass.states.get(entity)
        unit = live.attributes.get("unit_of_measurement") if live else None
        samples: list[Sample] = []
        for state in data.get(entity, []):
            try:
                watts = to_watts(float(state.state), unit)
            except (TypeError, ValueError):
                continue  # unavailable/unknown - a fabricated zero would lie
            if watts is not None:
                samples.append((state.last_changed, watts))
        out[entity] = samples
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

     THE SETTLE WINDOW HAS TO OUTLAST YOUR METER'S REPORTING INTERVAL. If it
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
    watching: set[str] = set()
    for entity in entities:
        watching.update(automations_with_entity(hass, entity))
    for auto_id in watching:
        state = hass.states.get(auto_id)
        snapshot[auto_id] = state.attributes.get("last_triggered") if state else None
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
    watching: set[str] = set()
    for entity in entities:
        watching.update(automations_with_entity(hass, entity))
    # Sorted so the refused list keeps a stable order across runs - it is
    # surfaced to the caller, and set order is not deterministic.
    for auto_id in sorted(watching):
        state = hass.states.get(auto_id)
        if state is None or state.state != "on":
            continue  # already off; leave it alone and do not re-enable it later
        referenced = set(entities_in_automation(hass, auto_id))
        classes = {
            e: state.attributes.get("device_class")
            if (state := hass.states.get(e))
            else None
            for e in referenced
        }
        ok, why = safe_to_pause(referenced, classes)
        if ok:
            pausable.append(auto_id)
        else:
            refused.append({"automation": auto_id, "reason": why})
    return sorted(pausable), refused


def _own_meter(hass: HomeAssistant, entity_id: str) -> str | None:
    """The device's own power sensor, if it has one.

     THE SINGLE BIGGEST ACCURACY LEVER IN ACTIVE PROBING, AND ASKING THE USER
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


def _label_name(title: str, prefix: str) -> str:
    """A label for a circuit: its own name, with the prefix only if it needs one.

    Meter firmware titles ("EmporiaVue Circuit 16 Power") get trimmed to the
    part a person would recognise; energy-dashboard names ("Circuit 16 Study")
    are already right and are used as they are.
    """
    cleaned = " ".join(
        w for w in title.split() if not w.lower().startswith("emporiavue")
    )
    for noise in (" Power", " Energy"):
        if cleaned.endswith(noise):
            cleaned = cleaned[: -len(noise)]
    cleaned = cleaned.strip()
    if not cleaned:
        return prefix
    if cleaned.lower().startswith(prefix.lower()):
        return cleaned
    return f"{prefix} {cleaned}".strip()


def _entry_id(hass: HomeAssistant) -> str | None:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].entry_id if entries else None


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


def _response(payload: dict[str, Any]) -> ServiceResponse:
    """A service response is JSON; mypy cannot see that through nesting."""
    return cast("ServiceResponse", payload)


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions at component setup, independent of any entry."""

    async def _learn(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        coordinator = runtime.coordinator
        store = runtime.store

        circuits = call.data.get("circuits") or coordinator.circuits
        days = call.data["days"]
        threshold = (
            call.data.get("threshold") or coordinator.profile["cluster_threshold"]
        )

        histories = await _histories(hass, list(circuits), days)

        def _crunch(entity: str, samples: list[Sample]) -> list[Fingerprint]:
            events = segment(samples)
            if not events:
                return []
            rows = [e.as_features() for e in events]
            labels = cluster(normalize(rows), threshold=threshold)
            # Each shape's own rhythm, learned from when its runs actually
            # happened. This is what absence detection judges against later.
            rhythms = cadences_by_cluster(labels, [e.start for e in events])
            return summarize(entity, rows, labels, rhythms)

        report: dict[str, list[dict[str, Any]]] = {}
        for entity in circuits:
            samples = histories.get(entity) or []
            if not samples:
                # Empty history is UNREAD, not "nothing ran" - say which.
                report[entity] = [{"error": "no history in window"}]
                continue
            # Pure CPU over days of samples - segmenting and clustering a
            # 27-circuit panel on the event loop stalled all of Home Assistant.
            fps = await hass.async_add_executor_job(_crunch, entity, samples)
            if not fps:
                report[entity] = []
                continue
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
        return _response({"circuits": report})

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
        probes = call.data.get("probes") or int(coordinator.profile["probes"])
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
        # Refuse to cut a live load. See safe_to_switch_off - the domain
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

        # A settle window shorter than the meter's own reporting interval
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
            # Record before switching off. Dying between the two leaves a
            # stale record that restores something already running, which is
            # harmless; the other order leaves automations off silently.
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

                metered = base_device is not None and active_device is not None
                if base_device is not None and active_device is not None:
                    expected = active_device - base_device
                else:
                    # Without the device's own meter, fall back to the largest
                    # coherent circuit change. Weaker, and flagged as such.
                    # Only circuits readable in BOTH windows count: one that
                    # went unreadable mid-probe would otherwise read as a step
                    # to 0 W and poison the expected magnitude.
                    expected = max(
                        (
                            active_circuits[c] - base_circuits[c]
                            for c in base_circuits.keys() & active_circuits.keys()
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
                        # The actual condition, not `expected is not None` -
                        # that was true on both branches, so every unmetered
                        # probe was graded as if the device had a meter.
                        "device_metered": metered,
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
            # RESTORE IS IN `finally`, NOT ON THE HAPPY PATH. A probe that
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
        supports_response=SupportsResponse.OPTIONAL,
    )

    async def _map(call: ServiceCall) -> ServiceResponse:
        """Work out which circuit each self-metering device sits on.

         THIS WAS TOOL-ONLY UNTIL NOW. The analysis lived in
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

        raw_circuits: dict[str, list[Sample]] = await _histories(hass, circuits, days)

        # The grid must be about one reporting interval wide, and that is a
        # property of the meter, not a constant. Measured from this install's
        # own history unless the caller overrides it.
        measured = [
            v for v in (sample_interval(rows) for rows in raw_circuits.values()) if v
        ]
        measured.sort()
        step = call.data.get("step") or (
            max(1, round(measured[len(measured) // 2])) if measured else 12
        )

        ctraces = await hass.async_add_executor_job(
            lambda: {
                c: resample(rows, step, start, end) for c, rows in raw_circuits.items()
            }
        )
        filled = sum(1 for t in ctraces.values() if t)
        if not filled:
            # Empty history is UNREAD, not "no matches". Say which.
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_history",
                translation_placeholders={"count": str(len(circuits))},
            ) from None

        # Three kinds of sensor look like devices and are not: this
        # integration's own output (`unmonitored_load` is mains minus circuits,
        # so correlating it is circular), panel aggregates such as phase and
        # per-unit totals, and anything else sharing a device with a configured
        # circuit - which is the general form of the second.
        registry = er.async_get(hass)

        circuit_devices = set()
        for circuit in circuits:
            row = registry.async_get(circuit)
            if row and row.device_id:
                circuit_devices.add(row.device_id)

        own_entry_id = _entry_id(hass)
        known = {*circuits, coordinator.mains}
        candidates: list[str] = []
        for state in hass.states.async_all("sensor"):
            if state.entity_id in known:
                continue
            attrs = state.attributes
            if attrs.get("device_class") != "power":
                continue
            if attrs.get("state_class") != "measurement":
                continue
            row = registry.async_get(state.entity_id)
            if row is not None:
                if row.config_entry_id == own_entry_id:
                    continue  # (1) our own derived sensors
                if row.device_id and row.device_id in circuit_devices:
                    continue  # (2)/(3) panel aggregates and phase legs
            candidates.append(state.entity_id)

        device_histories = await _histories(hass, candidates, days)

        p = coordinator.profile
        min_r = call.data.get("min_correlation") or p["min_correlation"]

        def _crunch() -> dict[str, dict[str, Any]]:
            # Resampling ~40 device traces onto a 6-second grid and the
            # iterative re-scoring in assign() are minutes of pure CPU on a
            # real install - on the event loop that stalled every automation
            # and dashboard in the house for the duration.
            devices = {
                entity: trace
                for entity, rows in device_histories.items()
                if (trace := resample(rows, step, start, end))
            }
            return assign(
                devices,
                ctraces,
                min_r=min_r,
                min_step_match=p["min_step_match"],
                min_margin=p["min_margin"],
            )

        result = await hass.async_add_executor_job(_crunch)

        # Areas are REPORTED, NEVER SCORED. Circuit identity is what this is
        # trying to learn, so feeding Home Assistant's own area names back into
        # the scoring would be circular. They are here to make a wrong answer
        # visible, which is exactly how an over-wide grid was caught.
        areas = ar.async_get(hass)
        device_reg = dr.async_get(hass)

        def _area(entity_id: str) -> str | None:
            """An entity's area, falling back to its device's.

             `entity.area_id` is only set when someone has OVERRIDDEN the
            area on that specific entity. Almost every entity inherits its
            area from its device, so reading the entity alone returned None for
            every single device on the first real run - silently disabling the
            one cross-check that makes a wrong mapping visible.
            """
            row = registry.async_get(entity_id)
            if row is None:
                return None
            area_id = row.area_id
            if area_id is None and row.device_id:
                device = device_reg.async_get(row.device_id)
                area_id = device.area_id if device else None
            if area_id is None:
                return None
            area = areas.async_get_area(area_id)
            return area.name if area else None

        placed: list[dict[str, Any]] = []
        unplaced: list[dict[str, Any]] = []
        quiet: list[dict[str, Any]] = []
        for name, info in result.items():
            placement = {
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
                placed.append(placement)
            elif str(info.get("reason", "")) == "no transition in window":
                quiet.append(placement)
            else:
                unplaced.append(placement)

        # A circuit claiming devices from three unrelated areas is a smell, and
        # printing it is the only reason the over-wide grid was ever noticed.
        per_circuit: dict[str, set[str]] = {}
        for placement in placed:
            area_name = str(placement["area"]) if placement["area"] else "?"
            per_circuit.setdefault(str(placement["circuit"]), set()).add(area_name)
        suspect = sorted(c for c, a in per_circuit.items() if len(a) >= 3)

        # Passive results are recorded too, but never over a probe's answer.
        wrote = 0
        for placement in placed:
            if await runtime.store.async_record_assignment(
                str(placement["device"]),
                str(placement["circuit"]),
                # Always INFERRED: "measured" is reserved for active probes,
                # and a strong history correlation is still a correlation.
                INFERRED,
                "correlation",
                {
                    "step_match": placement["step_match"],
                    "correlation": placement["correlation"],
                    "margin": placement["margin"],
                    "grid_seconds": step,
                },
            ):
                wrote += 1
        if wrote:
            await coordinator.async_request_refresh()

        return _response(
            {
                "assignments_recorded": wrote,
                "grid_seconds": step,
                "grid_source": "given" if call.data.get("step") else "measured",
                "days": days,
                "circuits_with_history": filled,
                "devices_examined": len(device_histories),
                "placed": sorted(placed, key=lambda r: -float(r["step_match"] or 0)),
                "unplaced": sorted(unplaced, key=lambda r: str(r["device"])),
                "no_transition_in_window": sorted(
                    quiet, key=lambda r: str(r["device"])
                ),
                "circuits_claiming_three_or_more_areas": suspect,
            }
        )

    async def _autolabel(call: ServiceCall) -> ServiceResponse:
        """Name every shape whose circuit already says what it is.

         ONLY WHERE THERE IS NOTHING TO GUESS. Two conditions, both strict:
        the circuit's own title must contain an appliance name somebody typed,
        and the circuit must have exactly ONE learned shape. A circuit called
        "Dish Washer" with a single recurring shape has one answer. A circuit
        called "Circuit 25" has none, and a circuit with three shapes needs a
        human to say which is which - naming the biggest would be a guess
        wearing a fact's clothes.

        Never overwrites a name a person gave.
        """
        runtime = _runtime(hass)
        store = runtime.store
        # The energy dashboard's names beat the entity titles: they are what a
        # person typed rather than what the firmware shipped.
        dashboard_names = await async_circuit_names(hass)
        by_circuit: dict[str, list[Fingerprint]] = {}
        for fp in store.fingerprints:
            by_circuit.setdefault(fp.circuit, []).append(fp)

        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for circuit, shapes in sorted(by_circuit.items()):
            state = hass.states.get(circuit)
            title = dashboard_names.get(circuit) or (
                str(state.attributes.get("friendly_name", "")) if state else ""
            )
            suggestion = suggest_label(title) if title else None
            unnamed = [f for f in shapes if not is_named(f)]
            if suggestion is None:
                skipped.append(
                    {"circuit": circuit, "why": "circuit has no appliance name"}
                )
                continue
            if len(shapes) != 1:
                skipped.append(
                    {
                        "circuit": circuit,
                        "why": (
                            f"{len(shapes)} shapes - a human must say which is which"
                        ),
                        "suggestion": suggestion,
                    }
                )
                continue
            if not unnamed:
                skipped.append({"circuit": circuit, "why": "already named"})
                continue
            ok = await store.async_relabel(circuit, unnamed[0].label, suggestion)
            if ok:
                applied.append({"circuit": circuit, "named": suggestion})

        if applied:
            await runtime.coordinator.async_request_refresh()
        return _response({"named": applied, "left_for_you": skipped})

    hass.services.async_register(DOMAIN, SERVICE_LABEL, _label, schema=LABEL_SCHEMA)

    async def _apply_labels(call: ServiceCall) -> ServiceResponse:
        """Label each mapped device with the circuit it sits on.

         WHY LABELS AND NOT THE OBVIOUS THING. `via_device` is what Home
        Assistant means by "related" - 149 devices on the development install
        already use it - but an integration may only set it on devices IT
        OWNS, and these belong to ZHA and Z-Wave. Creating a device per circuit
        was the alternative and was rejected on Lewis's objection: 27 entries
        called "Circuit 30" beside 519 real ones read as duplicate devices, and
        that registry already has `emporiavue` and `EmporiaVue` confusing
        people.

         Labels are the USER'S namespace and no integration API for them is
        documented - there is simply no ownership check stopping this. So it
        defaults to a dry run, only ever ADDS to a device's existing labels,
        and `remove: true` takes every label back off.
        """
        runtime = _runtime(hass)
        devices = dr.async_get(hass)
        entities = er.async_get(hass)
        labels = lr.async_get(hass)
        dashboard_names = await async_circuit_names(hass)
        prefix = str(call.data["prefix"]).strip()
        dry = call.data["dry_run"]

        planned: list[dict[str, Any]] = []
        for device_entity, row in sorted(runtime.store.assignments().items()):
            circuit = str(row.get("circuit") or "")
            if not circuit:
                continue
            entity_row = entities.async_get(device_entity)
            if entity_row is None or entity_row.device_id is None:
                continue
            state = hass.states.get(circuit)
            title = dashboard_names.get(circuit) or (
                str(state.attributes.get("friendly_name", "")) if state else circuit
            )
            # A label wants the breaker number, unlike an appliance name, so
            # dashboard names are used whole.
            name = _label_name(title, prefix)
            planned.append(
                {
                    "device_entity": device_entity,
                    "device_id": entity_row.device_id,
                    "label": name,
                    "established_by": row.get("source"),
                }
            )

        if dry:
            return _response(
                {
                    "dry_run": True,
                    "would_label": planned,
                    "note": (
                        "Nothing was changed. Call again with dry_run: false "
                        "to apply, or remove: true to take them off."
                    ),
                }
            )

        touched = 0
        for item in planned:
            device = devices.async_get(str(item["device_id"]))
            if device is None:
                continue
            existing = lr.async_get(hass).async_get_label_by_name(str(item["label"]))
            if call.data["remove"]:
                if existing is None or existing.label_id not in device.labels:
                    continue
                devices.async_update_device(
                    device.id, labels=device.labels - {existing.label_id}
                )
                touched += 1
                continue
            if existing is None:
                existing = labels.async_create(str(item["label"]))
            if existing.label_id in device.labels:
                continue
            # ADD, never replace - the other labels on that device are
            # somebody else's and none of our business.
            devices.async_update_device(
                device.id, labels=device.labels | {existing.label_id}
            )
            touched += 1

        return _response(
            {
                "dry_run": False,
                "removed" if call.data["remove"] else "labelled": touched,
                "devices": planned,
            }
        )

    hass.services.async_register(
        DOMAIN,
        SERVICE_LABELS,
        _apply_labels,
        schema=LABELS_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_AUTOLABEL,
        _autolabel,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_MAP,
        _map,
        schema=MAP_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_VERIFY,
        _verify,
        schema=VERIFY_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
