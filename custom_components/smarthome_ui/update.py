"""The dashboard as an update in Home Assistant (Settings → Updates)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.update import UpdateEntity, UpdateEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CoreConfigEntry
from .entity import DEVICE_SYSTEM, CoreEntity
from .ui import SIGNAL_UI, UiError, UiManager


async def async_setup_entry(
    hass: HomeAssistant, entry: CoreConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    """Set up the update of the dashboard."""
    async_add_entities([UiUpdate(entry.entry_id, entry.runtime_data.ui)])


class UiUpdate(CoreEntity, UpdateEntity):
    """The dashboard card Core serves (update.system_dashboard)."""

    _attr_translation_key = "dashboard"
    _attr_supported_features = UpdateEntityFeature.INSTALL | UpdateEntityFeature.RELEASE_NOTES
    _attr_title = "Smarthome UI"

    def __init__(self, entry_id: str, ui: UiManager) -> None:
        """Initialize."""
        super().__init__(entry_id, "ui_dashboard", DEVICE_SYSTEM, "update", "dashboard")
        self._ui = ui

    async def async_added_to_hass(self) -> None:
        """Follow checks and installs."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UI, self.async_write_ha_state)
        )

    @property
    def installed_version(self) -> str | None:
        """Return the installed card."""
        return self._ui.data.get("installed")

    @property
    def latest_version(self) -> str | None:
        """Return the newest card (the installed one if not checked yet)."""
        return self._ui.data.get("latest") or self.installed_version

    @property
    def in_progress(self) -> bool:
        """Return True while installing."""
        return self._ui.busy

    async def async_install(self, version: str | None, backup: bool, **kwargs: Any) -> None:
        """Install the newest card."""
        try:
            await self._ui.async_install()
        except UiError as err:
            raise HomeAssistantError(f"Smarthome UI: {err}") from err

    async def async_release_notes(self) -> str | None:
        """Return what is new since the installed version."""
        return self._ui.notes() or None
