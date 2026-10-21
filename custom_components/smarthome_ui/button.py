"""Button per display: reload the dashboard."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CoreConfigEntry
from .displays import DisplayEntity, async_setup_display_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one reload button per display."""
    manager = entry.runtime_data.displays
    entry.async_on_unload(
        async_setup_display_entities(
            hass,
            entry.entry_id,
            manager,
            async_add_entities,
            lambda did: [DisplayReload(entry.entry_id, manager, did, "reload", "button")],
        )
    )


class DisplayReload(DisplayEntity, ButtonEntity):
    """Reload the dashboard on this display."""

    async def async_press(self) -> None:
        """Send the reload command."""
        self._command({"type": "reload"})
