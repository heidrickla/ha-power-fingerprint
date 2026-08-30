"""Sensors: unmonitored load, standby power, standby cost."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import MIN_STANDBY_WINDOW_HOURS
from .coordinator import FingerprintCoordinator, PowerFingerprintConfigEntry
from .entity import AttachedEntity, FingerprintEntity

# See binary_sensor.py - everything is derived from state already in memory.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PowerFingerprintConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data.coordinator
    store = entry.runtime_data.store

    async_add_entities(
        [
            UnmonitoredLoadSensor(coordinator),
            StandbyPowerSensor(coordinator),
            StandbyCostSensor(coordinator),
            CandidatesSensor(coordinator),
        ]
    )

    # An appliance sensor is only meaningful once a circuit has at least one
    # NAMED fingerprint, so they are added as labelling happens rather than
    # creating one per configured circuit up front. On a 27-circuit panel that
    # would be 27 entities permanently reading "unknown", which is entity spam
    # dressed up as a feature.
    known: set[str] = set()

    @callback
    def _add_new_circuits() -> None:
        new = [c for c in store.circuits() if c not in known]
        if not new:
            return
        known.update(new)
        async_add_entities(ApplianceSensor(coordinator, c) for c in new)

    _add_new_circuits()
    entry.async_on_unload(coordinator.async_add_listener(_add_new_circuits))

    # One entity per mapped device, attached to THAT device rather than to this
    # integration's service device - see AttachedEntity. Added as mappings are
    # established, so a probe run populates device pages without a restart.
    mapped: set[str] = set()

    @callback
    def _add_mapped_devices() -> None:
        new = []
        for device_entity in store.assignments():
            if device_entity in mapped:
                continue
            info = _device_info_for(hass, device_entity)
            if info is None:
                # Not in the registry, or not on a device. Nothing to attach to.
                continue
            mapped.add(device_entity)
            new.append(CircuitSensor(coordinator, device_entity, info))
        if new:
            async_add_entities(new)

    _add_mapped_devices()
    entry.async_on_unload(coordinator.async_add_listener(_add_mapped_devices))


def _device_info_for(hass: HomeAssistant, entity_id: str) -> DeviceInfo | None:
    """The DeviceInfo needed to attach an entity to an existing device.

    Carries only the target device's own identifiers and connections. Anything
    else - a name, a manufacturer - would be this integration writing to a
    device it does not own.
    """
    entities = er.async_get(hass)
    row = entities.async_get(entity_id)
    if row is None or row.device_id is None:
        return None
    device = dr.async_get(hass).async_get(row.device_id)
    if device is None:
        return None
    return DeviceInfo(
        identifiers=set(device.identifiers),
        connections=set(device.connections),
    )


class _Base(FingerprintEntity, SensorEntity):
    """Sensor flavour of the shared base."""


class _StandbyBase(_Base):
    """For the two sensors that are only meaningful once the window has filled.

    ⛔ Refuses to answer rather than answering wrongly. A 5th percentile over
    four minutes is not a rough standby figure, it is a different quantity
    wearing the same label, and it reads as authoritative on a dashboard.
    """

    @property
    def _window_ready(self) -> bool:
        hours = (self.coordinator.data or {}).get("window_hours", 0.0)
        return float(hours) >= MIN_STANDBY_WINDOW_HOURS

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        hours = (self.coordinator.data or {}).get("window_hours", 0.0)
        return {
            "window_hours": hours,
            "window_ready": self._window_ready,
            "minimum_window_hours": MIN_STANDBY_WINDOW_HOURS,
        }


class ApplianceSensor(_Base):
    """What is running on one circuit right now.

    States are `idle`, `starting`, an appliance name, or `unknown`. The last
    is not a failure - it means the circuit is drawing power in a shape no
    named fingerprint accounts for, which is worth surfacing rather than
    forcing into the nearest bucket.

    ⚠ NOT a `SensorDeviceClass.ENUM`. An enum sensor has to declare its full
    option list up front, and the whole point of this one is that the set of
    appliances grows as they are learned and named.
    """

    _attr_translation_key = "appliance"

    def __init__(self, coordinator: FingerprintCoordinator, circuit: str) -> None:
        super().__init__(coordinator, f"appliance_{circuit}")
        self._circuit = circuit
        # The circuit's own friendly name where it has one, so the entity reads
        # "Dishwasher circuit appliance" rather than repeating an entity id.
        state = coordinator.hass.states.get(circuit)
        pretty = (
            state.attributes.get("friendly_name")
            if state and state.attributes.get("friendly_name")
            else circuit.split(".", 1)[-1].replace("_", " ")
        )
        self._attr_translation_placeholders = {"circuit": str(pretty)}

    @property
    def _info(self) -> dict[str, Any]:
        return ((self.coordinator.data or {}).get("running") or {}).get(
            self._circuit
        ) or {}

    @property
    def native_value(self) -> str | None:
        value = self._info.get("state")
        return str(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        info = dict(self._info)
        info.pop("state", None)
        info["circuit"] = self._circuit
        return info


class UnmonitoredLoadSensor(_Base):
    """Mains minus the sum of the circuits.

    On a fully clamped panel this sits near zero. A step change is the useful
    signal: a clamp came off, a CT reversed, or a load appeared on a circuit
    nobody is measuring.
    """

    _attr_translation_key = "unmonitored_load"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "unmonitored_load")

    @property
    def native_value(self) -> float | None:
        cov = (self.coordinator.data or {}).get("coverage")
        return cov["unmonitored_w"] if cov else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        cov = data.get("coverage") or {}
        return {
            "mains_w": cov.get("mains_w"),
            "circuits_w": cov.get("circuits_w"),
            "coverage_pct": cov.get("coverage_pct"),
            "silent_circuits": data.get("silent_circuits", []),
        }


class StandbyPowerSensor(_StandbyBase):
    """Total permanent draw across every monitored circuit.

    This is always-on load, not waste - a rack at a flat 640 W is a legitimate
    640 W. The ranking in the attributes is the actionable part.
    """

    _attr_translation_key = "standby_power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "standby_power")

    @property
    def native_value(self) -> float | None:
        if not self._window_ready:
            return None
        return (self.coordinator.data or {}).get("standby_total_w")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Top 15 only: the attribute payload is written to the state machine on
        # every update and a 36-circuit ranking would bloat the recorder.
        ranking = (self.coordinator.data or {}).get("standby", [])
        return {**super().extra_state_attributes, "ranking": ranking[:15]}


class StandbyCostSensor(_StandbyBase):
    """Annualised cost of that permanent draw, at the configured price.

    ⚠ DELIBERATELY NOT `SensorDeviceClass.MONETARY`. That device class means
    money actually accumulated and Home Assistant requires it to carry a
    `total` state class. This is a projection of a rate - it moves down as well
    as up, and nothing has been spent - so claiming it is monetary would put a
    forecast into cost dashboards as though it were a bill.
    """

    _attr_translation_key = "standby_annual_cost"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "standby_annual_cost")

    @property
    def native_unit_of_measurement(self) -> str | None:
        # The user's own currency, not a hardcoded one. Home Assistant knows it
        # from the general settings, and a fixed "USD" would be wrong for most
        # of the people who install this.
        currency = self.hass.config.currency
        return str(currency) if currency else None

    @property
    def native_value(self) -> float | None:
        if not self._window_ready:
            return None
        return (self.coordinator.data or {}).get("standby_annual_cost")


class CandidatesSensor(_Base):
    """Learned shapes waiting for a human to say what they are.

    ⭐ WITHOUT THIS THE LEARN STEP HAS NO VISIBLE OUTPUT. Clustering finds the
    recurring shapes and genuinely cannot name them - that needs someone who
    knows what is plugged in - but until now the candidates existed only in
    `.storage` and in the `learn` action's response. A person who ran `learn`
    and then looked at the integration saw six entities and no sign that
    anything had been learned at all. On the development install that was 39
    shapes, entirely invisible.

    The attributes carry the plain-English description of each shape, which is
    the thing a human actually reads to recognise "that's the dishwasher".
    """

    _attr_translation_key = "candidates"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    # Capped because every attribute payload is written to the state machine on
    # each update, and an unbounded list on a large panel would bloat the
    # recorder. The count in the state is always the true total.
    _MAX_SHOWN = 25

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "candidates")

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data
        if data is None:
            return None
        return len(data.get("candidates", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data or {}
        rows = data.get("candidates", [])
        out: dict[str, Any] = {
            "named": data.get("labelled_fingerprints", 0),
            "candidates": rows[: self._MAX_SHOWN],
        }
        if len(rows) > self._MAX_SHOWN:
            out["not_shown"] = len(rows) - self._MAX_SHOWN
        if rows:
            # The exact call that names one, so nobody has to go and read the
            # docs to act on what this sensor is telling them.
            first = rows[0]
            out["to_name_one"] = (
                f"action: power_fingerprint.label / circuit: {first['circuit']} / "
                f"current_label: {first['candidate']} / new_label: <what it is>"
            )
        return out


class CircuitSensor(AttachedEntity, SensorEntity):
    """Which breaker this device is actually on, shown on the device itself.

    The state is the circuit sensor's friendly name, because "Circuit 30" on
    the front porch light's own page is the answer to a question somebody asked
    while standing at a breaker panel. The attributes carry how it was
    established, because ⛔ a passive correlation and a three-probe agreement
    are not the same claim and must never look the same.
    """

    _attr_translation_key = "circuit"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: FingerprintCoordinator,
        device_entity: str,
        device_info: DeviceInfo,
    ) -> None:
        super().__init__(coordinator, f"circuit_{device_entity}", device_info)
        self._device_entity = device_entity

    @property
    def _row(self) -> dict[str, Any]:
        store = self.coordinator.store
        if store is None:
            return {}
        row: dict[str, Any] = store.assignments().get(self._device_entity, {})
        return row

    @property
    def native_value(self) -> str | None:
        circuit = self._row.get("circuit")
        if not circuit:
            return None
        state = self.hass.states.get(str(circuit))
        if state and state.attributes.get("friendly_name"):
            return str(state.attributes["friendly_name"])
        return str(circuit)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        row = self._row
        return {
            "measured_entity": self._device_entity,
            "circuit_entity": row.get("circuit"),
            "confidence": row.get("confidence"),
            # `probe` switched the device and watched a circuit move.
            # `correlation` only observed them moving together.
            "established_by": row.get("source"),
            "evidence": row.get("evidence", {}),
        }
