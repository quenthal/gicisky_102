"""Native binary sensors fed by the bridge's result/status topics."""
from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity, BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .entity import BridgeStatusEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    panel, name = data["panel"], data["name"]
    async_add_entities([
        SyncedBinarySensor(panel, name),
        ConnectivityBinarySensor(panel, name),
    ])


class SyncedBinarySensor(BridgeStatusEntity, BinarySensorEntity):
    """ON = last push completed cleanly (the bridge's `ok`); details as attributes.

    `ok` is the bridge's verdict: done 0508/0503, or the compression2 past-end
    exit where done stays "0500" -- pinning to 0508 would show those as OFF."""

    _topic_kind = "result"
    _attr_name = "Synkassa"
    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, panel, name):
        super().__init__(panel, name)
        self._attr_unique_id = f"{DOMAIN}_{panel}_synced"
        self._attr_is_on = None
        self._attr_extra_state_attributes = {}

    def _apply(self, data: dict[str, Any]) -> None:
        self._attr_is_on = bool(data.get("ok"))
        self._attr_extra_state_attributes = {
            "parts": data.get("parts"),
            "fmt": data.get("fmt"),
            "mean_ms": data.get("mean_ms"),
            "total_s": data.get("total_s"),
            "tried_s": data.get("tried_s"),
            "reason": data.get("reason"),
            "done": data.get("done"),
            "md5_ok": data.get("md5_ok"),
        }


class ConnectivityBinarySensor(BridgeStatusEntity, BinarySensorEntity):
    """ON = bridge reports online (retained status topic)."""

    _topic_kind = "status"
    _attr_name = "Yhteys"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, panel, name):
        super().__init__(panel, name)
        self._attr_unique_id = f"{DOMAIN}_{panel}_connectivity"
        self._attr_is_on = None

    def _apply(self, data: dict[str, Any]) -> None:
        self._attr_is_on = data.get("status") == "online"
