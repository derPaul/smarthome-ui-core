"""Switch to pause all notifications (e.g. on holiday)."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import CoreConfigEntry
from .const import SIGNAL_NOTIFY
from .entity import DEVICE_NOTIFY, CoreEntity
from .store import CoreStore


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the switch."""
    async_add_entities([NotificationsSwitch(entry.entry_id, entry.runtime_data.store)])


class NotificationsSwitch(CoreEntity, SwitchEntity):
    """Notifications on or paused."""

    _attr_name = None  # main entity of the device: switch.<device name>
    _attr_translation_key = "notifications"  # icon

    def __init__(self, entry_id: str, store: CoreStore) -> None:
        """Initialize the switch."""
        super().__init__(entry_id, "notifications", DEVICE_NOTIFY, "switch")
        self._store = store

    @property
    def is_on(self) -> bool:
        """Return True if notifications are sent."""
        return bool(self._store.data["notify"].get("enabled", True))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Resume notifications."""
        self._store.set_notify({"enabled": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Pause notifications."""
        self._store.set_notify({"enabled": False})

    async def async_added_to_hass(self) -> None:
        """Update when the setting changes."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_NOTIFY, self.async_write_ha_state)
        )
