"""Binary sensors: is the dashboard open on a display."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import CoreConfigEntry
from .displays import DisplayEntity, async_setup_display_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one connectivity sensor per display."""
    manager = entry.runtime_data.displays
    entry.async_on_unload(
        async_setup_display_entities(
            hass,
            entry.entry_id,
            manager,
            async_add_entities,
            lambda did: [DisplayOnline(entry.entry_id, manager, did, "online", "binary_sensor")],
        )
    )


class DisplayOnline(DisplayEntity, BinarySensorEntity):
    """The dashboard is open and connected."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    @property
    def is_on(self) -> bool:
        """Return True while the dashboard is open."""
        return self._online

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return when the display was last seen."""
        seen = self._record.get("last_seen")
        return {"last_seen": dt_util.utc_from_timestamp(seen / 1000).isoformat() if seen else None}
