"""Common base for the entities: one service device per area (notifications, household, garden)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN

DEVICE_NOTIFY = "notifications"
DEVICE_HOUSEHOLD = "household"
DEVICE_GARDEN = "garden"
DEVICES = (DEVICE_NOTIFY, DEVICE_HOUSEHOLD, DEVICE_GARDEN)


def attach_device(entity: Entity, hass: HomeAssistant, device_id: str | None) -> None:
    """Put an entity on an existing device of another integration (mower, plant, dishwasher …).

    Home Assistant matches the identifiers of a ``DeviceInfo`` only within the own config entry,
    so a ``DeviceInfo`` with the other device's identifiers would create a second, empty device
    named after this integration. Setting ``device_entry`` attaches to the device itself.
    """
    if device_id and (device := dr.async_get(hass).async_get(device_id)) is not None:
        entity.device_entry = device


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
