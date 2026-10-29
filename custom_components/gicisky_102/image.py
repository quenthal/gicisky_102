"""Image entity: shows the last image rendered for the panel."""
from __future__ import annotations

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import DOMAIN, signal_image
from .entity import device_info


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([GiciskyImage(hass, data["panel"], data["name"])])


class GiciskyImage(ImageEntity):
    """Holds the most recently rendered panel image."""

    _attr_has_entity_name = True
    _attr_name = None  # use the device name
    _attr_content_type = "image/png"

    def __init__(self, hass: HomeAssistant, panel: str, name: str) -> None:
        super().__init__(hass)
        self._panel = panel
        self._image_bytes: bytes | None = None
        self._attr_unique_id = f"{DOMAIN}_{panel}_image"
        self._attr_device_info = device_info(panel, name)
        self._unsub = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._unsub = async_dispatcher_connect(
            self.hass, signal_image(self._panel), self._handle_new_image
        )

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _handle_new_image(self, png_bytes: bytes) -> None:
        self._image_bytes = png_bytes
        self._attr_image_last_updated = dt_util.utcnow()
        self.async_write_ha_state()

    async def async_image(self) -> bytes | None:
        return self._image_bytes
