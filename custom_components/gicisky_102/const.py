"""Constants for the gicisky_102 integration (10.2" BWR panel via MQTT bridge)."""
from __future__ import annotations

DOMAIN = "gicisky_102"

# Panel geometry (fixed for the 10.2" BWR panel).
PANEL_WIDTH = 960
PANEL_HEIGHT = 640

# Config / option keys.
CONF_PANEL_ID = "panel_id"
CONF_NAME = "name"
CONF_COMPRESS = "compress"
CONF_THRESHOLD = "threshold"
CONF_RED_THRESHOLD = "red_threshold"

DEFAULT_PANEL_ID = "008b"
DEFAULT_NAME = "Gicisky 10.2\""
# Compression2/QuickLZ is the default (6-bit hash encoder + bridge past-end commit
# grace): ~45-70 KB / ~185-300 parts vs 162 KB / 678 parts per-column.
DEFAULT_COMPRESS = True
DEFAULT_THRESHOLD = 128
DEFAULT_RED_THRESHOLD = 170

# MQTT topics (relative to the panel id). The ESP bridge owns the other end.
def topic_push(panel: str) -> str:
    return f"gicisky/{panel}/cmd/push"


def topic_battery(panel: str) -> str:
    return f"gicisky/{panel}/battery"


def topic_result(panel: str) -> str:
    return f"gicisky/{panel}/result"


def topic_status(panel: str) -> str:
    return f"gicisky/{panel}/status"


# Dispatcher signal used to push bridge status updates to sensor entities.
def signal_update(panel: str) -> str:
    return f"{DOMAIN}_update_{panel}"


# Dispatcher signal used to push a freshly rendered image to the image entity.
def signal_image(panel: str) -> str:
    return f"{DOMAIN}_image_{panel}"


# Dispatcher signal used to push the write lifecycle status to the status sensor.
def signal_status(panel: str) -> str:
    return f"{DOMAIN}_status_{panel}"


# Write lifecycle (Tila sensor). pending is a separate boolean attribute, not a value.
STATUS_IDLE = "idle"
STATUS_SENDING = "sending"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_TIMEOUT = "timeout"
STATUS_OPTIONS = [STATUS_IDLE, STATUS_SENDING, STATUS_DONE, STATUS_FAILED, STATUS_TIMEOUT]

# Safety valve: if no result arrives within this, release the lock (silent ESP crash,
# dropped MQTT, etc.). Sized well above the worst-case ESP push (cold-wake + readiness
# polling + 678-part transfer + recovery), which can approach ~3 min.
WRITE_TIMEOUT_S = 300


# Platforms this integration sets up.
PLATFORMS = ["image", "sensor", "binary_sensor"]

# Service.
SERVICE_WRITE = "write"
