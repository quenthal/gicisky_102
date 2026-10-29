"""Native sensors fed by the bridge's battery topic."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorEntity, SensorDeviceClass, SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE, UnitOfElectricPotential, EntityCategory,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, STATUS_IDLE, STATUS_OPTIONS, signal_status
from .entity import BridgeStatusEntity, device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    panel, name = data["panel"], data["name"]
    async_add_entities([
        BatterySensor(panel, name),
        VoltageSensor(panel, name),
        SignalSensor(panel, name),
        FirmwareSensor(panel, name),
        StatusSensor(panel, name),
    ])


class _BatteryTopicSensor(BridgeStatusEntity, SensorEntity):
    _topic_kind = "battery"
    _key = ""

    def _apply(self, data: dict[str, Any]) -> None:
        if self._key in data:
            self._attr_native_value = data[self._key]


class BatterySensor(_BatteryTopicSensor):
    _key = "batt_pct"
    _attr_name = "Akku"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, panel, name):
        super().__init__(panel, name)
        self._attr_unique_id = f"{DOMAIN}_{panel}_battery"


class VoltageSensor(_BatteryTopicSensor):
    _key = "batt_v"
    _attr_name = "Jännite"
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, panel, name):
        super().__init__(panel, name)
        self._attr_unique_id = f"{DOMAIN}_{panel}_voltage"


class SignalSensor(_BatteryTopicSensor):
    _key = "rssi"
    _attr_name = "Signaali"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = "dBm"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, panel, name):
        super().__init__(panel, name)
        self._attr_unique_id = f"{DOMAIN}_{panel}_rssi"


class FirmwareSensor(_BatteryTopicSensor):
    _key = "fw"
    _attr_name = "Firmware"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, panel, name):
        super().__init__(panel, name)
        self._attr_unique_id = f"{DOMAIN}_{panel}_fw"


class StatusSensor(SensorEntity):
    """Write lifecycle: idle -> sending -> done/failed/timeout.

    Carries a `pending` boolean attribute (a coalesced update is waiting for the
    panel to free up), plus `reason`/`parts` from the last result."""

    _attr_has_entity_name = True
    _attr_name = "Tila"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS_OPTIONS

    def __init__(self, panel: str, name: str) -> None:
        self._panel = panel
        self._attr_unique_id = f"{DOMAIN}_{panel}_status"
        self._attr_device_info = device_info(panel, name)
        self._attr_native_value = STATUS_IDLE
        self._attr_extra_state_attributes = {"pending": False}
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        self._unsub = async_dispatcher_connect(
            self.hass, signal_status(self._panel), self._update
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _update(self, data: dict) -> None:
        self._attr_native_value = data.get("status", self._attr_native_value)
        attrs = {"pending": data.get("pending", False)}
        if data.get("reason") is not None:
            attrs["reason"] = data["reason"]
        if data.get("parts") is not None:
            attrs["parts"] = data["parts"]
        self._attr_extra_state_attributes = attrs
        self.async_write_ha_state()
