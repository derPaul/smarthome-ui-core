"""Binary sensors: is the dashboard open on a display; is an appliance waiting to be emptied."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from . import CoreConfigEntry
from .appliance import ApplianceEntity, ApplianceTracker, async_setup_appliance_entities
from .const import SIGNAL_SECURITY
from .displays import DisplayEntity, async_setup_display_entities
from .entity import DEVICE_SECURITY, CoreEntity


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
    security = entry.runtime_data.security
    async_add_entities(
        [
            SecurityAllAsleep(entry.entry_id, security),
            *(
                PersonAsleep(entry.entry_id, security, p["entity"], p["name"])
                for p in security.presence()
            ),
        ]
    )
    async_setup_appliance_entities(
        entry.runtime_data.appliances,
        async_add_entities,
        lambda t: [ApplianceToUnload(entry.entry_id, t)] if t.unloads else [],
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


class ApplianceToUnload(ApplianceEntity, BinarySensorEntity):
    """The run is over and the appliance waits to be emptied."""

    def __init__(self, entry_id: str, tracker: ApplianceTracker) -> None:
        """Initialize (binary_sensor.household_<appliance>_to_unload)."""
        super().__init__(entry_id, tracker, "to_unload", "binary_sensor")

    @property
    def is_on(self) -> bool:
        """Return True while it waits to be emptied."""
        return self._tracker.to_unload()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return since when."""
        finished = self._tracker.info()["finished"]
        return {
            "since": dt_util.utc_from_timestamp(finished / 1000).isoformat()
            if finished and self.is_on
            else None
        }


class _SecurityBinary(CoreEntity, BinarySensorEntity):
    """A binary sensor of the device "Sicherheit"."""

    async def async_added_to_hass(self) -> None:
        """Update when persons or phones change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SECURITY, self.async_write_ha_state)
        )


class SecurityAllAsleep(_SecurityBinary):
    """Somebody is home and everybody at home sleeps."""

    _attr_translation_key = "security_all_asleep"

    def __init__(self, entry_id: str, security: Any) -> None:
        """Initialize (binary_sensor.security_all_asleep)."""
        super().__init__(
            entry_id, "security_all_asleep", DEVICE_SECURITY, "binary_sensor", "all_asleep"
        )
        self._security = security

    @property
    def is_on(self) -> bool:
        """Return True if all at home sleep."""
        return self._security.all_asleep()


class PersonAsleep(_SecurityBinary):
    """A person sleeps: at home, phone charging at night for a while."""

    _attr_translation_key = "security_asleep"

    def __init__(self, entry_id: str, security: Any, person: str, name: str) -> None:
        """Initialize (binary_sensor.security_<person>_asleep)."""
        obj = slugify(person.split(".", 1)[1])
        super().__init__(
            entry_id, f"security_asleep_{obj}", DEVICE_SECURITY, "binary_sensor", f"{obj}_asleep"
        )
        self._security = security
        self._person = person
        self._attr_translation_placeholders = {"person": name}

    def _me(self) -> dict[str, Any]:
        return next((p for p in self._security.presence() if p["entity"] == self._person), {})

    @property
    def is_on(self) -> bool:
        """Return True while the person sleeps."""
        return bool(self._me().get("asleep"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return whether the person is home and since when they sleep."""
        me = self._me()
        since = me.get("since")
        return {
            "person": self._person,
            "home": me.get("home"),
            "since": dt_util.utc_from_timestamp(since / 1000).isoformat() if since else None,
        }
