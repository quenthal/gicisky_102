"""Gicisky 10.2" panel via an ESP32 MQTT bridge.

A dedicated, BLE-free integration for the one large panel the upstream gicisky
integration cannot drive over BLE. Rendering happens inside HA (real hass -> plot,
history, fonts all work); the rendered image is encoded and published over MQTT to an
ESP32 bridge that pushes it to the panel over BLE.

Keep the upstream `gicisky` integration for the small (BLE) panels; this one stays
separate and does not touch it.
"""
from __future__ import annotations

import io
import logging

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import (
    async_dispatcher_send, async_dispatcher_connect,
)
from homeassistant.helpers.event import async_call_later

from . import mqtt_bridge, render
from .const import (
    DOMAIN,
    PLATFORMS,
    SERVICE_WRITE,
    CONF_PANEL_ID,
    CONF_COMPRESS,
    CONF_THRESHOLD,
    CONF_RED_THRESHOLD,
    DEFAULT_COMPRESS,
    DEFAULT_THRESHOLD,
    DEFAULT_RED_THRESHOLD,
    signal_image,
    signal_status,
    signal_update,
    topic_result,
    STATUS_IDLE,
    STATUS_SENDING,
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_TIMEOUT,
    WRITE_TIMEOUT_S,
)

_LOGGER = logging.getLogger(__name__)


def _payload_list(value):
    """Require a native list of element dicts -- same as the upstream integration.

    Templates must render to a list (`{{ els }}`), NOT a JSON string
    (`{{ els | to_json }}`); the renderer iterates the list directly."""
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise vol.Invalid(
            "payload must be a list of element dicts. In a template use "
            "{{ els }} (a list), not {{ els | to_json }} (a string)."
        )
    return value


WRITE_SCHEMA = vol.Schema(
    {
        vol.Required("payload"): _payload_list,
        vol.Optional(CONF_PANEL_ID): cv.string,
        # No schema default: when omitted, the entry's Configure option decides
        # (changeable in the UI without restarting Home Assistant).
        vol.Optional(CONF_COMPRESS): cv.boolean,
        vol.Optional(CONF_THRESHOLD, default=DEFAULT_THRESHOLD): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional(CONF_RED_THRESHOLD, default=DEFAULT_RED_THRESHOLD): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=255)
        ),
        vol.Optional("rotate", default=0): vol.In([0, 90, 180, 270]),
        vol.Optional("background", default="white"): cv.string,
    }
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a panel from a config entry."""
    panel = entry.data[CONF_PANEL_ID]
    hass.data.setdefault(DOMAIN, {})

    unsubs = await mqtt_bridge.async_subscribe(hass, panel)

    hass.data[DOMAIN][entry.entry_id] = {
        "panel": panel,
        "name": entry.title,
        "unsubs": unsubs,
        "status": STATUS_IDLE,   # write lifecycle (the Tila sensor + busy lock)
        "pending": None,         # coalesced pending request (max 1, latest wins)
        "timeout_cancel": None,  # safety-valve timer handle
        # Default encoding when the service call omits `compress` -- comes from
        # the Configure dialog (options) and is applied live via the reload
        # listener below, no HA restart needed.
        "compress_default": entry.options.get(CONF_COMPRESS, DEFAULT_COMPRESS),
    }

    # Re-run setup when options change so the new default takes effect immediately.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Listen to the bridge's result topic to release the lock (done/failed).
    # Hop onto the loop so the handler (and the timer cancel / task creation it does)
    # is thread-safe even if the dispatch arrives off-loop.
    unsub_result = async_dispatcher_connect(
        hass, signal_update(panel),
        lambda topic, data: hass.loop.call_soon_threadsafe(
            _handle_bridge_message, hass, entry.entry_id, topic, data
        ),
    )
    hass.data[DOMAIN][entry.entry_id]["unsubs"].append(unsub_result)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_write_service(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, None)
        if data:
            if data.get("timeout_cancel"):
                data["timeout_cancel"]()
            for unsub in data.get("unsubs", []):
                unsub()
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, SERVICE_WRITE)
    return ok


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply changed options live (no restart)."""
    rt = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if rt:
        rt["compress_default"] = entry.options.get(CONF_COMPRESS, DEFAULT_COMPRESS)
        _LOGGER.info("gicisky_102: compress default for panel %s is now %s",
                     rt["panel"], rt["compress_default"])


def _resolve_entry_id(hass: HomeAssistant, call: ServiceCall) -> str:
    """Resolve which entry to write to: explicit panel_id, else the only entry."""
    explicit = call.data.get(CONF_PANEL_ID)
    entries = hass.data.get(DOMAIN, {})
    by_panel = {d["panel"]: eid for eid, d in entries.items()}
    if explicit:
        if explicit not in by_panel:
            raise HomeAssistantError(f"Unknown panel_id '{explicit}'")
        return by_panel[explicit]
    if len(by_panel) == 1:
        return next(iter(by_panel.values()))
    raise HomeAssistantError(
        "Multiple panels configured; specify panel_id in the service call."
    )


def _params_from_call(call: ServiceCall, compress_default: bool) -> dict:
    return {
        "payload": call.data["payload"],
        # per-call value wins; otherwise the entry's Configure option decides
        "compress": call.data.get(CONF_COMPRESS, compress_default),
        "threshold": call.data[CONF_THRESHOLD],
        "red_threshold": call.data[CONF_RED_THRESHOLD],
        "rotate": call.data["rotate"],
        "background": call.data["background"],
    }


@callback
def _set_status(hass: HomeAssistant, entry_id: str, status: str,
                extra: dict | None = None) -> None:
    """Update runtime status and notify the Tila sensor (with pending attribute)."""
    rt = hass.data[DOMAIN][entry_id]
    rt["status"] = status
    payload = {"status": status, "pending": rt.get("pending") is not None}
    if extra:
        payload.update(extra)
    # dispatch on the loop so the sensor update is thread-safe from any caller
    hass.loop.call_soon_threadsafe(
        async_dispatcher_send, hass, signal_status(rt["panel"]), payload
    )


@callback
def _cancel_timeout(rt: dict) -> None:
    if rt.get("timeout_cancel"):
        rt["timeout_cancel"]()
        rt["timeout_cancel"] = None


@callback
def _arm_timeout(hass: HomeAssistant, entry_id: str) -> None:
    rt = hass.data[DOMAIN][entry_id]
    _cancel_timeout(rt)
    rt["timeout_cancel"] = async_call_later(
        hass, WRITE_TIMEOUT_S,
        callback(lambda _now: _on_timeout(hass, entry_id)),
    )


@callback
def _on_timeout(hass: HomeAssistant, entry_id: str) -> None:
    rt = hass.data[DOMAIN].get(entry_id)
    if not rt or rt["status"] != STATUS_SENDING:
        return
    rt["timeout_cancel"] = None
    _LOGGER.warning(
        "gicisky_102: no result for panel %s within %ss; releasing (timeout)",
        rt["panel"], WRITE_TIMEOUT_S,
    )
    _set_status(hass, entry_id, STATUS_TIMEOUT, {"reason": "timeout"})
    _maybe_start_pending(hass, entry_id)


@callback
def _handle_bridge_message(hass: HomeAssistant, entry_id: str,
                           topic: str, data: dict) -> None:
    """On a result message, release the lock (done/failed) and start any pending."""
    rt = hass.data[DOMAIN].get(entry_id)
    if not rt or topic != topic_result(rt["panel"]):
        return
    if rt["status"] != STATUS_SENDING:
        return  # ignore late/unsolicited results (small race; see notes)
    _cancel_timeout(rt)
    # Trust the bridge's verdict: it already validates the end status (0508/0503
    # via the done branch, or the past-end exit where done stays "0500"). Pinning
    # to done == "0508" here would mark successful compression2 pushes as failed.
    ok = bool(data.get("ok"))
    _set_status(
        hass, entry_id, STATUS_DONE if ok else STATUS_FAILED,
        {"reason": data.get("reason"), "parts": data.get("parts")},
    )
    _maybe_start_pending(hass, entry_id)


@callback
def _maybe_start_pending(hass: HomeAssistant, entry_id: str) -> None:
    rt = hass.data[DOMAIN][entry_id]
    pending = rt.get("pending")
    if pending:
        rt["pending"] = None
        hass.async_create_task(_do_write(hass, entry_id, pending))


async def _do_write(hass: HomeAssistant, entry_id: str, params: dict) -> None:
    """Render, publish, claim the lock, and arm the safety-valve timeout.

    Claims the lock (status=sending) synchronously before the first await so a
    concurrent service call sees the panel busy and coalesces instead of racing."""
    rt = hass.data[DOMAIN][entry_id]
    panel = rt["panel"]
    _set_status(hass, entry_id, STATUS_SENDING)
    _arm_timeout(hass, entry_id)

    try:
        image = await hass.async_add_executor_job(
            lambda: render.render(
                hass, params["payload"],
                rotate=params["rotate"], background=params["background"],
            )
        )
        png = await hass.async_add_executor_job(_to_png, image)
        async_dispatcher_send(hass, signal_image(panel), png)
        nchunks = await mqtt_bridge.publish_image(
            hass, panel, image,
            compress=params["compress"],
            threshold=params["threshold"], red_threshold=params["red_threshold"],
        )
        _LOGGER.info("gicisky_102: sent %d chunks to panel %s (compress=%s)",
                     nchunks, panel, params["compress"])
    except Exception as err:
        # render/publish failure: release the lock so we don't hang in 'sending'
        _cancel_timeout(rt)
        _LOGGER.error("gicisky_102: write to panel %s failed: %s", panel, err)
        _set_status(hass, entry_id, STATUS_FAILED, {"reason": f"write_error: {err}"})
        _maybe_start_pending(hass, entry_id)


def _async_register_write_service(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_WRITE):
        return

    async def _handle_write(call: ServiceCall) -> None:
        entry_id = _resolve_entry_id(hass, call)
        rt = hass.data[DOMAIN][entry_id]
        params = _params_from_call(call, rt.get("compress_default", DEFAULT_COMPRESS))

        if rt["status"] == STATUS_SENDING:
            # panel busy: coalesce. Keep only the latest request (max 1); re-render
            # happens when it actually fires, so plot/history is fresh at send time.
            rt["pending"] = params
            _set_status(hass, entry_id, STATUS_SENDING)  # refresh pending attribute
            _LOGGER.info("gicisky_102: panel %s busy; coalesced pending update",
                         rt["panel"])
            return

        await _do_write(hass, entry_id, params)

    hass.services.async_register(DOMAIN, SERVICE_WRITE, _handle_write, schema=WRITE_SCHEMA)


def _to_png(image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    return buf.getvalue()
