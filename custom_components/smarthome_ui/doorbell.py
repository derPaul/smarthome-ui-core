"""Doorbells linked by their device: push with camera picture, last ring, mute switch.

A doorbell is added as subentry ("Add doorbell") by picking its device (UniFi Protect, Reolink,
Ring, Nuki …). On the device Core finds the ring (an ``event`` entity of device class doorbell,
else a doorbell ``binary_sensor``), the camera (high resolution channel, not the package camera)
and the person sensor; the lock is the one chosen or the one in the same area.

When it rings: notification "Es klingelt: <name>" (source ``sec.bell``) with the camera picture –
urgent if nobody is home – unless the switch "Klingel-Mitteilung" of the doorbell is off. The last
rings are kept (``doorbells``: ``{id: {"rings": [ms, ...], "mute": bool}}``).
"""

from __future__ import annotations

from collections.abc import Callable
import re
from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from .const import SIGNAL_DOORBELL
from .entity import attach_device
from .mower import CONF_DEVICE
from .store import CoreStore, now_ms

SUBENTRY_DOORBELL = "doorbell"
RINGS_MAX = 30
INVALID = ("unknown", "unavailable", "")
_BELL = re.compile(r"(doorbell|klingel|ring)", re.IGNORECASE)
_NOT_BELL = re.compile(r"(chime|gong|volume|lautst|repeat)", re.IGNORECASE)
_PACKAGE = re.compile(r"(package|paket)", re.IGNORECASE)
_HIGH = re.compile(r"(high|hoch|main|haupt|clear)", re.IGNORECASE)


class DoorbellTracker:
    """One configured doorbell."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, subentry: ConfigSubentry) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.store = store
        self.id = subentry.subentry_id
        self.name = subentry.title
        self.device_id: str = subentry.data[CONF_DEVICE]
        self._camera: str | None = subentry.data.get("camera") or None
        self._lock: str | None = subentry.data.get("lock") or None
        self.on_ring: Callable[[DoorbellTracker], None] | None = None

    def entities(self) -> list[er.RegistryEntry]:
        """Return the entities of the doorbell device (not our own, not disabled)."""
        return [
            e
            for e in er.async_entries_for_device(er.async_get(self.hass), self.device_id)
            if e.platform != "smarthome_ui" and not e.disabled_by
        ]

    def _text(self, entry: er.RegistryEntry) -> str:
        state = self.hass.states.get(entry.entity_id)
        name = state.attributes.get("friendly_name", "") if state else ""
        return f"{entry.translation_key or ''} {entry.entity_id} {name}"

    @property
    def ring_entity(self) -> str | None:
        """Return the ring: doorbell event (preferred) or doorbell binary sensor."""
        events, binaries = [], []
        for entry in self.entities():
            state = self.hass.states.get(entry.entity_id)
            attrs = state.attributes if state else {}
            if entry.domain == "event" and (
                attrs.get("device_class") == "doorbell"
                or entry.original_device_class == "doorbell"
                or any(re.search(r"^ring$|doorbell", str(t)) for t in attrs.get("event_types", []))
            ):
                events.append(entry.entity_id)
            elif (
                entry.domain == "binary_sensor"
                and _BELL.search(self._text(entry))
                and not _NOT_BELL.search(self._text(entry))
            ):
                binaries.append(entry.entity_id)
        return next(iter(events + binaries), None)

    @property
    def camera(self) -> str | None:
        """Return the camera (chosen, else the main channel of the device)."""
        if self._camera:
            return self._camera
        cams = [e.entity_id for e in self.entities() if e.domain == "camera"]
        live = [c for c in cams if (s := self.hass.states.get(c)) and s.state not in INVALID]
        for group in (live, cams):
            main = [c for c in group if not _PACKAGE.search(c)]
            if hit := next((c for c in main if _HIGH.search(c)), None) or next(iter(main), None):
                return hit
        return None

    @property
    def area_id(self) -> str | None:
        """Return the area of the doorbell device."""
        device = dr.async_get(self.hass).async_get(self.device_id)
        return device.area_id if device else None

    @property
    def lock(self) -> str | None:
        """Return the lock (chosen, else one in the same area)."""
        if self._lock:
            return self._lock
        area = self.area_id
        if not area:
            return None
        registry, devices = er.async_get(self.hass), dr.async_get(self.hass)
        for state in self.hass.states.async_all("lock"):
            entry = registry.async_get(state.entity_id)
            if entry is None:
                continue
            device = devices.async_get(entry.device_id) if entry.device_id else None
            if (entry.area_id or (device.area_id if device else None)) == area:
                return state.entity_id
        return None

    def _data(self) -> dict[str, Any]:
        return self.store.data["doorbells"].setdefault(self.id, {"rings": [], "mute": False})

    @property
    def muted(self) -> bool:
        """Return True if the ring notification is off."""
        return bool(self._data().get("mute"))

    @callback
    def set_muted(self, muted: bool) -> None:
        """Switch the ring notification off / on."""
        self._data()["mute"] = muted
        self.store.save()
        async_dispatcher_send(self.hass, SIGNAL_DOORBELL, self.id)

    def rings(self) -> list[int]:
        """Return the last rings (ms, oldest first)."""
        return list(self._data().get("rings") or [])

    def rings_today(self) -> int:
        """Return how often it rang today."""
        start = dt_util.start_of_local_day().timestamp() * 1000
        return sum(1 for t in self.rings() if t >= start)

    @callback
    def rang(self) -> None:
        """It rang: remember it and tell (unless muted)."""
        rings = self._data().setdefault("rings", [])
        rings.append(now_ms())
        del rings[:-RINGS_MAX]
        self.store.save()
        async_dispatcher_send(self.hass, SIGNAL_DOORBELL, self.id)
        if not self.muted and self.on_ring is not None:
            self.on_ring(self)

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        rings = self.rings()
        return {
            "id": self.id,
            "name": self.name,
            "device_id": self.device_id,
            "ring": self.ring_entity,
            "camera": self.camera,
            "lock": self.lock,
            "last_ring": rings[-1] if rings else None,
            "rings_today": self.rings_today(),
            "muted": self.muted,
        }


class DoorbellManager:
    """All configured doorbells."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self.trackers: dict[str, DoorbellTracker] = {}
        self.on_ring: Callable[[DoorbellTracker], None] | None = None
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_setup(self, subentries: list[ConfigSubentry]) -> None:
        """Start listening to the rings."""
        for subentry in subentries:
            if subentry.subentry_type == SUBENTRY_DOORBELL:
                tracker = DoorbellTracker(self.hass, self.store, subentry)
                tracker.on_ring = self.on_ring
                self.trackers[tracker.id] = tracker
        if not self.trackers:
            return

        @callback
        def relevant(data: EventStateChangedData) -> bool:
            return any(t.ring_entity == data["entity_id"] for t in self.trackers.values())

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            old, new = event.data["old_state"], event.data["new_state"]
            if new is None or new.state in INVALID or old is None:
                return
            eid = event.data["entity_id"]
            if eid.startswith("event."):
                rang = new.state != old.state  # a new event time
            else:
                rang = new.state == "on" and old.state != "on"
            if not rang:
                return
            for tracker in self.trackers.values():
                if tracker.ring_entity == eid:
                    tracker.rang()

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=relevant)
        )

    @callback
    def async_stop(self) -> None:
        """Stop listening."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def info(self) -> list[dict[str, Any]]:
        """Return the doorbells for the dashboard."""
        return [t.info() for t in self.trackers.values()]


class DoorbellEntity(Entity):
    """An entity on the doorbell's own device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, tracker: DoorbellTracker, key: str, domain: str) -> None:
        """Initialize (entity ID: <domain>.security_<doorbell>_<key>)."""
        self._tracker = tracker
        self._attr_unique_id = f"{entry_id}_doorbell_{tracker.id}_{key}"
        self._attr_translation_key = f"doorbell_{key}"
        self.entity_id = f"{domain}.security_{slugify(tracker.name)}_{key}"
        attach_device(self, tracker.hass, tracker.device_id)

    async def async_added_to_hass(self) -> None:
        """Update when it rings or the switch changes."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_DOORBELL, self._changed))

    @callback
    def _changed(self, doorbell_id: str) -> None:
        if doorbell_id == self._tracker.id:
            self.async_write_ha_state()


@callback
def async_setup_doorbell_entities(
    manager: DoorbellManager,
    async_add_entities: AddConfigEntryEntitiesCallback,
    create: Callable[[DoorbellTracker], list[Entity]],
) -> None:
    """Add the entities of every doorbell."""
    for tracker in manager.trackers.values():
        async_add_entities(create(tracker), config_subentry_id=tracker.id)
