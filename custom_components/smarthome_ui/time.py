"""Time: when the good night check runs."""

from __future__ import annotations

from datetime import time
from typing import Any

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CoreConfigEntry
from .const import SIGNAL_SECURITY
from .entity import DEVICE_SECURITY, CoreEntity


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the time of the good night check."""
    async_add_entities([GoodNightTime(entry.entry_id, entry.runtime_data.security)])


class GoodNightTime(CoreEntity, TimeEntity):
    """When the good night check looks at open windows, locks and the alarm."""

    _attr_translation_key = "security_good_night"

    def __init__(self, entry_id: str, security: Any) -> None:
        """Initialize (time.security_good_night)."""
        super().__init__(entry_id, "security_good_night", DEVICE_SECURITY, "time", "good_night")
        self._security = security

    @property
    def native_value(self) -> time | None:
        """Return the time of the check."""
        hour, _, minute = str(self._security.checks()["good_night_at"]).partition(":")
        try:
            return time(int(hour), int(minute or 0))
        except ValueError:
            return None

    async def async_set_value(self, value: time) -> None:
        """Change the time of the check."""
        self._security.set_check("good_night_at", value.strftime("%H:%M"))

    async def async_added_to_hass(self) -> None:
        """Update when the settings change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SECURITY, self.async_write_ha_state)
        )
