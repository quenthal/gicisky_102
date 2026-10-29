"""Load the encoder modules without a Home Assistant install.

mqtt_bridge.py only needs a few HA names at import time; they are stubbed here,
and the package is registered by path so its __init__ (voluptuous, HA config
entries) is never imported.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "gicisky_102"


def _stub(name: str, **attrs) -> types.ModuleType:
    module = sys.modules.get(name) or types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


_mqtt = _stub("homeassistant.components.mqtt")
_stub("homeassistant")
_stub("homeassistant.core", HomeAssistant=object)
_stub("homeassistant.helpers")
_stub("homeassistant.helpers.dispatcher", async_dispatcher_send=lambda *a, **k: None)
_stub("homeassistant.components", mqtt=_mqtt)

_package = types.ModuleType("gicisky_102")
_package.__path__ = [str(COMPONENT)]
sys.modules.setdefault("gicisky_102", _package)
