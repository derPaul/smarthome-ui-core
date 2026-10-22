"""Selects: page, theme, season, sounds and performance per display; mowing frequency per area."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import CoreConfigEntry
from .const import SIGNAL_MOWER
from .displays import SELECT_KEYS, DisplayEntity, async_setup_display_entities
from .entity import attach_device
from .mower import FREQ_OPTIONS, MowerTracker


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
    mowers = entry.runtime_data.mowers
    known: set[tuple[str, str]] = set()

    @callback
    def add_areas(_mower: str | None = None) -> None:
        for tracker in mowers.trackers.values():
            new = [
                MowFrequencySelect(entry.entry_id, tracker, area)
                for area in tracker.areas
                if (tracker.id, area.lower()) not in known
            ]
            if new:
                known.update((tracker.id, s.area.lower()) for s in new)
                async_add_entities(new, config_subentry_id=tracker.id)

    add_areas()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_MOWER, add_areas))


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


class MowFrequencySelect(SelectEntity):
    """How often an area is mowed: often, normal, rarely or not (pause)."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "mow_frequency"
    _attr_options = FREQ_OPTIONS

    def __init__(self, entry_id: str, tracker: MowerTracker, area: str) -> None:
        """Initialize the select (entity ID: select.garden_<area>_frequency)."""
        self._tracker = tracker
        self.area = area
        self._attr_unique_id = f"{entry_id}_mower_{tracker.id}_{slugify(area)}_frequency"
        self._attr_translation_placeholders = {"area": area}
        self.entity_id = f"select.garden_{slugify(area)}_frequency"
        attach_device(self, tracker.hass, tracker.device_id)

    @property
    def current_option(self) -> str:
        """Return the frequency."""
        return self._tracker.frequency(self.area)

    async def async_select_option(self, option: str) -> None:
        """Change the frequency."""
        self._tracker.set_frequency(self.area, option)

    async def async_added_to_hass(self) -> None:
        """Update when the mowing plan changes."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_MOWER, self._changed))

    @callback
    def _changed(self, mower_id: str) -> None:
        if mower_id == self._tracker.id:
            self.async_write_ha_state()
