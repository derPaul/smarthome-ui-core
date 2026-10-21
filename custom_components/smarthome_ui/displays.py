"""Displays: every open dashboard (wall tablet, phone, browser) becomes a device.

A dashboard connects with ``display/connect`` (a websocket subscription that stays open as long
as the dashboard is open) and reports its state (page, theme, season, sounds, performance,
standby, user) with ``display/update``. Entities show that state; changing them sends a command
through the open subscription, the dashboard applies it and reports back.
"""

from __future__ import annotations

from collections.abc import Callable
from itertools import count
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from .const import DOMAIN, SIGNAL_DISPLAY_NEW
from .store import CoreStore, now_ms

# what a dashboard reports and what can be changed from Home Assistant
STATE_KEYS = ("page", "theme", "season", "sounds", "performance", "standby")
SELECT_KEYS = ("page", "theme", "season", "sounds", "performance")


def display_signal(display_id: str) -> str:
    """Return the signal sent when one display changes."""
    return f"{DOMAIN}_display_{display_id}"


class DisplayManager:
    """Keep track of the displays, their open connections and their state."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self._conns: dict[str, dict[int, Callable[[dict[str, Any]], None]]] = {}
        self._tokens = count()

    @property
    def displays(self) -> dict[str, dict[str, Any]]:
        """Return all known displays."""
        return self.store.data["displays"]

    def online(self, display_id: str) -> bool:
        """Return True if the dashboard is open on this display."""
        return bool(self._conns.get(display_id))

    @callback
    def connect(
        self,
        display_id: str,
        name: str,
        send: Callable[[dict[str, Any]], None],
        *,
        user: str | None = None,
        state: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> CALLBACK_TYPE:
        """Register an open dashboard; return the callback to call when it closes."""
        new = display_id not in self.displays
        record = self.displays.setdefault(
            display_id, {"name": name, "user": None, "state": {}, "options": {}}
        )
        self._merge(record, user, state, options)
        record["last_seen"] = now_ms()
        token = next(self._tokens)
        self._conns.setdefault(display_id, {})[token] = send
        self.store.save()
        if new:
            async_dispatcher_send(self.hass, SIGNAL_DISPLAY_NEW, display_id)
        else:
            async_dispatcher_send(self.hass, display_signal(display_id))

        @callback
        def disconnect() -> None:
            conns = self._conns.get(display_id, {})
            conns.pop(token, None)
            if display_id in self.displays:
                self.displays[display_id]["last_seen"] = now_ms()
                self.store.save()
                async_dispatcher_send(self.hass, display_signal(display_id))

        return disconnect

    @callback
    def update(
        self,
        display_id: str,
        *,
        user: str | None = None,
        state: dict[str, Any] | None = None,
        options: dict[str, Any] | None = None,
    ) -> None:
        """Take a state report of a display."""
        record = self.displays.get(display_id)
        if record is None:
            return
        self._merge(record, user, state, options)
        record["last_seen"] = now_ms()
        self.store.save()
        async_dispatcher_send(self.hass, display_signal(display_id))

    @staticmethod
    def _merge(
        record: dict[str, Any],
        user: str | None,
        state: dict[str, Any] | None,
        options: dict[str, Any] | None,
    ) -> None:
        if user is not None:
            record["user"] = user
        if state:
            record["state"].update({k: v for k, v in state.items() if k in STATE_KEYS})
        if options:
            record["options"].update(
                {k: [str(o) for o in v] for k, v in options.items() if isinstance(v, list)}
            )

    @callback
    def command(self, display_id: str, command: dict[str, Any]) -> None:
        """Send a command to the open dashboard(s) of a display."""
        conns = self._conns.get(display_id)
        if not conns:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="display_offline",
                translation_placeholders={
                    "display": self.displays.get(display_id, {}).get("name", display_id)
                },
            )
        for send in list(conns.values()):
            send(command)
        if command.get("type") == "set" and display_id in self.displays:
            # shown right away; the dashboard confirms with its next report
            self.displays[display_id]["state"][command["key"]] = command["value"]
            async_dispatcher_send(self.hass, display_signal(display_id))

    @callback
    def async_shutdown(self) -> None:
        """Ask open dashboards to connect again (the integration is unloaded or reloaded)."""
        for conns in self._conns.values():
            for send in list(conns.values()):
                send({"type": "reconnect"})
        self._conns.clear()

    @callback
    def remove(self, display_id: str) -> None:
        """Forget a display."""
        self.displays.pop(display_id, None)
        self._conns.pop(display_id, None)
        self.store.save()


@callback
def async_setup_display_entities(
    hass: HomeAssistant,
    entry_id: str,
    manager: DisplayManager,
    async_add_entities: AddConfigEntryEntitiesCallback,
    factory: Callable[[str], list[Entity]],
) -> CALLBACK_TYPE:
    """Add the entities of all displays now and of new displays later."""
    async_add_entities([e for did in manager.displays for e in factory(did)])

    @callback
    def added(display_id: str) -> None:
        async_add_entities(factory(display_id))

    return async_dispatcher_connect(hass, SIGNAL_DISPLAY_NEW, added)


class DisplayEntity(Entity):
    """An entity of one display device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry_id: str, manager: DisplayManager, display_id: str, key: str, domain: str
    ) -> None:
        """Initialize the entity (entity ID: <domain>.display_<name>_<key>)."""
        self._manager = manager
        self._display_id = display_id
        name = manager.displays[display_id]["name"]
        self._attr_unique_id = f"{entry_id}_display_{display_id}_{key}"
        self._attr_translation_key = f"display_{key}"
        self.entity_id = f"{domain}.display_{slugify(name) or display_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_display_{display_id}")},
            name=name,
            manufacturer="Smarthome UI",
            model="Dashboard",
        )

    @property
    def _record(self) -> dict[str, Any]:
        return self._manager.displays.get(self._display_id) or {
            "state": {},
            "options": {},
            "user": None,
        }

    @property
    def _online(self) -> bool:
        return self._manager.online(self._display_id)

    def _command(self, command: dict[str, Any]) -> None:
        self._manager.command(self._display_id, command)

    async def async_added_to_hass(self) -> None:
        """Update when the display reports."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, display_signal(self._display_id), self.async_write_ha_state
            )
        )
