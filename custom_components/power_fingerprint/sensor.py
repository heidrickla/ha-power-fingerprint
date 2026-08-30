"""Sensors: unmonitored load, standby power, standby cost."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import FingerprintCoordinator, PowerFingerprintConfigEntry
from .entity import FingerprintEntity

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


class _Base(FingerprintEntity, SensorEntity):
    """Sensor flavour of the shared base."""


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


class StandbyPowerSensor(_Base):
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
        return (self.coordinator.data or {}).get("standby_total_w")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        # Top 15 only: the attribute payload is written to the state machine on
        # every update and a 36-circuit ranking would bloat the recorder.
        ranking = (self.coordinator.data or {}).get("standby", [])
        return {"ranking": ranking[:15]}


class StandbyCostSensor(_Base):
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
        return (self.coordinator.data or {}).get("standby_annual_cost")
