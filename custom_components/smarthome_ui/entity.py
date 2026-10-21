"""Common base for the entities: one service device per area (notifications, household, garden)."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

DEVICE_NOTIFY = "notifications"
DEVICE_HOUSEHOLD = "household"
DEVICE_GARDEN = "garden"
DEVICES = (DEVICE_NOTIFY, DEVICE_HOUSEHOLD, DEVICE_GARDEN)


def device_for_source(src: str | None) -> str:
    """Return the device a task belongs to (garden calendar → garden, else household)."""
    return DEVICE_GARDEN if str(src or "").startswith("gd.") else DEVICE_HOUSEHOLD


class CoreEntity(Entity):
    """An entity of one of the Smarthome UI Core service devices."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry_id: str, key: str, device: str, domain: str, object_id: str | None = None
    ) -> None:
        """Initialize the entity.

        The entity ID is always English (``sensor.garden_…``, ``todo.household``), whatever
        language Home Assistant uses for the names shown.
        """
        self._attr_unique_id = f"{entry_id}_{key}"
        self.entity_id = f"{domain}.{device}" + (f"_{object_id}" if object_id else "")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_{device}")},
            translation_key=device,
            manufacturer="Smarthome UI",
            model="Smarthome UI Core",
            entry_type=DeviceEntryType.SERVICE,
        )
