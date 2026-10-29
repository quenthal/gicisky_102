"""Adapter around the vendored renderer.

Calls renderer.render_image() with the real hass object, so plot/history elements and
fonts resolve natively. render_image() expects a `device` (with .width/.height) and a
`service` (with .data). We build lightweight shims for those and return the PIL image.

render_image() is CPU-bound and may query the recorder (for `plot`); call render() via
hass.async_add_executor_job().
"""
from __future__ import annotations

from types import SimpleNamespace

from PIL import Image

from homeassistant.core import HomeAssistant

from .const import PANEL_WIDTH, PANEL_HEIGHT


class _Device:
    """Minimal stand-in for the integration's DeviceEntry (only dims are read)."""

    def __init__(self, width: int, height: int) -> None:
        self.width = width
        self.height = height


def render(hass: HomeAssistant, payload: list[dict], *,
           rotate: int = 0, background: str = "white",
           entity_id: str = "gicisky_102") -> Image.Image:
    """Render the element list to an RGB PIL image (PANEL_WIDTH x PANEL_HEIGHT).

    rotate stays 0 by default: we author landscape 960x640 and the encoder maps rows
    to panel columns. (renderer-level rotate is exposed for completeness.)"""
    from . import renderer  # heavy (PIL/barcode/qrcode); imported in executor only
    device = _Device(PANEL_WIDTH, PANEL_HEIGHT)
    service = SimpleNamespace(
        data={"payload": payload, "rotate": rotate, "background": background}
    )
    return renderer.render_image(entity_id, device, service, hass)
