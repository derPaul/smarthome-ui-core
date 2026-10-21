"""Selects per display: page, theme, season, sounds and performance mode."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CoreConfigEntry
from .displays import SELECT_KEYS, DisplayEntity, async_setup_display_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the selects of every display."""
    manager = entry.runtime_data.displays
    entry.async_on_unload(
        async_setup_display_entities(
            hass,
            entry.entry_id,
            manager,
            async_add_entities,
            lambda did: [
                DisplaySelect(entry.entry_id, manager, did, key, "select") for key in SELECT_KEYS
            ],
        )
    )


class DisplaySelect(DisplayEntity, SelectEntity):
    """One setting of a display; the options come from the dashboard."""

    @property
    def _key(self) -> str:
        return self._attr_translation_key.removeprefix("display_")

    @property
    def options(self) -> list[str]:
        """Return what the dashboard offers."""
        return list(self._record["options"].get(self._key) or [])

    @property
    def current_option(self) -> str | None:
        """Return the current value."""
        value = self._record["state"].get(self._key)
        return value if value in self.options else None

    async def async_select_option(self, option: str) -> None:
        """Change it on the display."""
        self._command({"type": "set", "key": self._key, "value": option})
