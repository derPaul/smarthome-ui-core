"""Common base for the entities."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class CoreEntity(Entity):
    """An entity of the Smarthome UI Core service device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, key: str) -> None:
        """Initialize the entity."""
        self._attr_unique_id = f"{entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Smarthome UI",
            manufacturer="Smarthome UI",
            entry_type=DeviceEntryType.SERVICE,
        )
