"""Shared device info and a dispatcher-fed base entity for bridge status."""
from __future__ import annotations

from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo, Entity

from .const import DOMAIN, signal_update, topic_battery, topic_result, topic_status


def device_info(panel: str, name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, panel)},
        name=name,
        manufacturer="Gicisky",
        model='EPD 10.2" BWR',
    )


class BridgeStatusEntity(Entity):
    """Base for entities fed by the bridge's battery/result/status MQTT topics."""

    _attr_has_entity_name = True
    _topic_kind: str = ""  # "battery" | "result" | "status"

    def __init__(self, panel: str, name: str) -> None:
        self._panel = panel
        self._attr_device_info = device_info(panel, name)
        self._unsub: Callable[[], None] | None = None

    async def async_added_to_hass(self) -> None:
        # Hop onto the loop so async_write_ha_state is thread-safe even if the
        # dispatch arrives off-loop (HA 2026 + py3.14 raise on off-loop updates).
        self._unsub = async_dispatcher_connect(
            self.hass, signal_update(self._panel),
            lambda topic, data: self.hass.loop.call_soon_threadsafe(
                self._dispatch, topic, data
            ),
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _dispatch(self, topic: str, data: dict[str, Any]) -> None:
        want = {
            "battery": topic_battery(self._panel),
            "result": topic_result(self._panel),
            "status": topic_status(self._panel),
        }[self._topic_kind]
        if topic != want:
            return
        self._apply(data)
        self.async_write_ha_state()

    def _apply(self, data: dict[str, Any]) -> None:
        raise NotImplementedError
