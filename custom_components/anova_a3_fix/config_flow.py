"""Config flow for the Anova A3 Delta Update Fix integration.

This integration has nothing to configure - it just needs to be enabled so
its __init__.py gets imported and applies the monkeypatch. The flow is a
single confirmation step, and only one instance is allowed.
"""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class AnovaA3FixConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Anova A3 Delta Update Fix."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            return self.async_create_entry(title="Anova A3 Delta Update Fix", data={})
        return self.async_show_form(step_id="user")
