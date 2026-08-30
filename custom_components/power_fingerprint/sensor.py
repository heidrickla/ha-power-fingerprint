"""Sensors: unmonitored load, standby power, standby cost."""

from __future__ import annotations

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import FingerprintCoordinator
from .entity import FingerprintEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator: FingerprintCoordinator = entry_data["coordinator"]
    store = entry_data["store"]

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


class ApplianceSensor(FingerprintEntity, SensorEntity):
    """What is running on one circuit right now.

    States are `idle`, `starting`, an appliance name, or `unknown`. The last
    is not a failure - it means the circuit is drawing power in a shape no
    named fingerprint accounts for, which is worth surfacing rather than
    forcing into the nearest bucket.
    """

    _attr_icon = "mdi:fingerprint"

    def __init__(self, coordinator: FingerprintCoordinator, circuit: str) -> None:
        super().__init__(coordinator, f"appliance_{circuit}")
        self._circuit = circuit
        pretty = circuit.split(".", 1)[-1].replace("_", " ")
        self._attr_name = f"{pretty} appliance"

    @property
    def _info(self) -> dict:
        return ((self.coordinator.data or {}).get("running") or {}).get(
            self._circuit
        ) or {}

    @property
    def native_value(self) -> str | None:
        return self._info.get("state")

    @property
    def extra_state_attributes(self) -> dict:
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
