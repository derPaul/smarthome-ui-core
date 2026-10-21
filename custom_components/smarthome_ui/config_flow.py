"""Config flow for Smarthome UI Core (nothing to configure – the dashboard does that)."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from .const import DOMAIN


class SmarthomeUiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Smarthome UI Core once."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm adding the integration."""
        if user_input is not None:
            return self.async_create_entry(title="Smarthome UI Core", data={})
        return self.async_show_form(step_id="user")
