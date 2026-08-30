"""Sensors: unmonitored load, standby power, standby cost."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FingerprintCoordinator
from .entity import FingerprintEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: FingerprintCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            UnmonitoredLoadSensor(coordinator),
            StandbyPowerSensor(coordinator),
            StandbyCostSensor(coordinator),
        ]
    )


class _Base(FingerprintEntity, SensorEntity):
    """Sensor flavour of the shared base."""


class UnmonitoredLoadSensor(_Base):
    """Mains minus the sum of the circuits.

    On a fully clamped panel this sits near zero. A step change is the useful
    signal: a clamp came off, a CT reversed, or a load appeared on a circuit
    nobody is measuring.
    """

    _attr_name = "Unmonitored load"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "unmonitored_load")

    @property
    def native_value(self) -> float | None:
        cov = (self.coordinator.data or {}).get("coverage")
        return cov["unmonitored_w"] if cov else None

    @property
    def extra_state_attributes(self) -> dict:
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

    _attr_name = "Standby power"
    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:sleep"

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "standby_power")

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("standby_total_w")

    @property
    def extra_state_attributes(self) -> dict:
        # Top 15 only: the attribute payload is written to the state machine on
        # every update and a 36-circuit ranking would bloat the recorder.
        ranking = (self.coordinator.data or {}).get("standby", [])
        return {"ranking": ranking[:15]}


class StandbyCostSensor(_Base):
    """Annualised cost of that permanent draw, at the configured price."""

    _attr_name = "Standby annual cost"
    _attr_native_unit_of_measurement = "USD"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-clock"

    def __init__(self, coordinator: FingerprintCoordinator) -> None:
        super().__init__(coordinator, "standby_annual_cost")

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("standby_annual_cost")
