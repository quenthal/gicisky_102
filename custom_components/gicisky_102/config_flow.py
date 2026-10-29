"""Config flow for gicisky_102."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    DOMAIN,
    CONF_PANEL_ID,
    CONF_NAME,
    CONF_COMPRESS,
    DEFAULT_PANEL_ID,
    DEFAULT_NAME,
    DEFAULT_COMPRESS,
)


class GiciskyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for a single 10.2" panel."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "GiciskyOptionsFlow":
        return GiciskyOptionsFlow()

    async def async_step_user(self, user_input=None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            panel = user_input[CONF_PANEL_ID].strip().lower()
            await self.async_set_unique_id(f"{DOMAIN}_{panel}")
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={CONF_PANEL_ID: panel, CONF_NAME: user_input[CONF_NAME]},
            )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=DEFAULT_NAME): cv.string,
                vol.Required(CONF_PANEL_ID, default=DEFAULT_PANEL_ID): cv.string,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)


class GiciskyOptionsFlow(OptionsFlow):
    """Runtime options (no restart needed): default encoding format."""

    async def async_step_init(self, user_input=None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_COMPRESS, DEFAULT_COMPRESS)
        schema = vol.Schema(
            {vol.Required(CONF_COMPRESS, default=current): cv.boolean}
        )
        return self.async_show_form(step_id="init", data_schema=schema)
