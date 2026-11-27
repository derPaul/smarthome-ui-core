"""Assistant for new devices: what was just paired, and what it still needs.

Devices that appear after Core is set up (an Eve Shutter Switch, a Zigbee sensor …) are kept as
"new" until done or dismissed in the dashboard. For each one Core suggests:

* **area** – from its name (an area name in it) or its parent device, else none,
* **name** – "<area> <kind>" when the device only has a model name ("Eve Shutter Switch 20EBN9901"
  → "Arbeitszimmer Rollladen"),
* **diagnostics** – its disabled diagnostic entities that help to find faults (signal, battery,
  reboots, uptime, Thread role …),
* **device class** – a cover without one becomes a shutter (blind for "Jalousie"), a contact
  without one a window (door when the name says so),
* **automations** – lights can join the presence simulation (and so the holiday mode); covers
  become windows by themselves (with their own morning and evening).

Applying writes to the registries and the presence simulation's settings.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_DATA, SIGNAL_NEWDEV
from .entity import all_devices
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

KEEP_DAYS = 14
# diagnostic entities worth having when something goes wrong
_DIAG = re.compile(
    r"rssi|lqi|linkquality|signal|batter|reboot|restart|uptime|last_seen|thread|role|"
    r"channel|neighbo|firmware|connect|availability|wifi|power_on|boot|error|fault"
)
_KIND = (
    ("cover", re.compile(r"jalousie|raffstore|blind"), "Jalousie"),
    ("cover", re.compile(r""), "Rollladen"),
    ("light", re.compile(r""), "Licht"),
    ("climate", re.compile(r""), "Thermostat"),
    ("lock", re.compile(r""), "Schloss"),
    ("media_player", re.compile(r""), "Lautsprecher"),
    ("binary_sensor", re.compile(r"door|tür|tuer"), "Tür"),
    ("binary_sensor", re.compile(r"window|fenster|contact|kontakt|opening"), "Fenster"),
    ("binary_sensor", re.compile(r"motion|occupancy|presence|bewegung|präsenz"), "Bewegungsmelder"),
    ("switch", re.compile(r""), "Steckdose"),
    ("sensor", re.compile(r"temp|humid|feucht"), "Klima-Sensor"),
)
# names that are only a model or a vendor ("Eve Shutter Switch 20EBN9901", "0x00158d0001a2b3c4")
_GENERIC = re.compile(
    r"^(0x[0-9a-f]+|[a-z0-9_-]*\d{4,}[a-z0-9_-]*)$|\b(eve|aqara|ikea|hue|shelly|"
    r"sonoff|tradfri|tuya|switch|sensor|plug|bulb|matter)\b",
    re.I,
)


class NewDevices:
    """Find new devices and apply what they still need."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, presence_sim: Any) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self.sim = presence_sim
        self._unsub: list[CALLBACK_TYPE] = []

    @property
    def data(self) -> dict[str, Any]:
        """Return {"since": ms, "known": [ids], "new": {id: ms}, "done": [ids]}."""
        return self.store.data.setdefault("newdev", {})

    def _save(self) -> None:
        self.store.save(SIGNAL_DATA)
        async_dispatcher_send(self.hass, SIGNAL_NEWDEV)

    # ---------------------------------------------------------------- tracking

    @callback
    def async_setup(self) -> None:
        """Remember the devices there are now; listen for new ones."""
        if not self.data.get("since"):
            # first start: what exists now is not new
            self.data.update({"since": now_ms(), "new": {}, "done": []})
            self.data["known"] = [d.id for d in all_devices(self.hass)]
            self._save()
        else:
            # devices paired while Home Assistant was down or Core was off
            known = set(self.data.get("known") or [])
            for device in all_devices(self.hass):
                if device.id not in known:
                    self._add(device.id)
        self._unsub.append(
            self.hass.bus.async_listen(dr.EVENT_DEVICE_REGISTRY_UPDATED, self._registry)
        )

    @callback
    def async_stop(self) -> None:
        """Stop listening."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _registry(self, event: Event) -> None:
        action = event.data.get("action")
        device_id = event.data.get("device_id")
        if action == "create" and device_id:
            self._add(device_id)
        elif action == "remove" and device_id in (self.data.get("new") or {}):
            self.data["new"].pop(device_id, None)
            self._save()

    @callback
    def _add(self, device_id: str) -> None:
        known = self.data.setdefault("known", [])
        if device_id not in known:
            known.append(device_id)
        device = dr.async_get(self.hass).async_get(device_id)
        # service devices (integrations, our own windows and displays) are not something you paired
        if device is None or device.entry_type is not None or self._ours(device):
            self._save()
            return
        self.data.setdefault("new", {})[device_id] = now_ms()
        self._save()

    def _ours(self, device: dr.DeviceEntry) -> bool:
        """Return True for a device of this integration (a window, a display …)."""
        return any(domain == DOMAIN for domain, _id in device.identifiers)

    def pending(self) -> list[str]:
        """Return the new devices not done yet (of the last 14 days)."""
        cutoff = now_ms() - KEEP_DAYS * 86_400_000
        devices = dr.async_get(self.hass)
        done = set(self.data.get("done") or [])
        return [
            did
            for did, t in sorted((self.data.get("new") or {}).items(), key=lambda x: -x[1])
            if t >= cutoff
            and did not in done
            and (device := devices.async_get(did)) is not None
            and not self._ours(device)
        ]

    # ---------------------------------------------------------------- suggestions

    def _entities(self, device_id: str) -> list[er.RegistryEntry]:
        return er.async_entries_for_device(
            er.async_get(self.hass), device_id, include_disabled_entities=True
        )

    def _kind(self, entries: list[er.RegistryEntry], text: str) -> str | None:
        domains = {e.domain for e in entries if e.entity_category is None}
        for domain, pattern, label in _KIND:
            if domain in domains:
                classes = " ".join(
                    str(e.device_class or e.original_device_class or "")
                    for e in entries
                    if e.domain == domain
                )
                if pattern.search(f"{text} {classes}".lower()):
                    return label
        return None

    def suggest(self, device_id: str) -> dict[str, Any] | None:
        """Return what Core suggests for a new device."""
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            return None
        areas = ar.async_get(self.hass)
        entries = self._entities(device_id)
        name = device.name_by_user or device.name or ""
        text = f"{name} {device.model or ''} {device.manufacturer or ''}"
        # area: already set, from the name, or from the parent device
        area_id = device.area_id
        if area_id is None:
            for area in sorted(areas.async_list_areas(), key=lambda a: -len(a.name)):
                if area.name.lower() in name.lower():
                    area_id = area.id
                    break
        if area_id is None and device.via_device_id:
            parent = dr.async_get(self.hass).async_get(device.via_device_id)
            area_id = parent.area_id if parent else None
        area = areas.async_get_area(area_id) if area_id else None
        kind = self._kind(entries, text)
        new_name = None
        if not device.name_by_user and area is not None and kind and _GENERIC.search(name):
            new_name = f"{area.name} {kind}"
        diagnostics = [
            {"entity_id": e.entity_id, "name": e.name or e.original_name or e.entity_id}
            for e in entries
            if e.disabled_by is er.RegistryEntryDisabler.INTEGRATION
            and _DIAG.search(f"{e.entity_id} {e.original_name or ''}".lower())
        ]
        classes = {}
        for e in entries:
            if e.device_class or e.original_device_class or e.entity_category is not None:
                continue
            if e.domain == "cover":
                classes[e.entity_id] = "blind" if kind == "Jalousie" else "shutter"
            elif e.domain == "binary_sensor" and kind in ("Fenster", "Tür"):
                classes[e.entity_id] = "door" if kind == "Tür" else "window"
        sim = self.sim.settings() if self.sim is not None else {}
        candidates = [
            e.entity_id
            for e in entries
            # covers are windows with their own automations, not part of the simulation
            if e.domain == "light" and e.entity_category is None and not e.disabled_by
        ]
        return {
            "device_id": device_id,
            "name": name,
            "model": device.model,
            "manufacturer": device.manufacturer,
            "added": (self.data.get("new") or {}).get(device_id),
            "area_id": device.area_id,
            "suggest_area": area_id if area_id != device.area_id else None,
            "suggest_name": new_name,
            "kind": kind,
            "diagnostics": diagnostics,
            "device_class": classes,
            "sim": [
                eid
                for eid in candidates
                if eid not in (sim.get("lights") or []) + (sim.get("covers") or [])
            ],
            "entities": [e.entity_id for e in entries if not e.disabled_by],
        }

    def info(self) -> dict[str, Any]:
        """Return the new devices with their suggestions."""
        return {"items": [s for did in self.pending() if (s := self.suggest(did))]}

    # ---------------------------------------------------------------- applying

    @callback
    def apply(self, device_id: str, choice: dict[str, Any]) -> dict[str, Any] | None:
        """Apply the chosen suggestions; the device is done afterwards."""
        sug = self.suggest(device_id)
        if sug is None:
            return None
        devices = dr.async_get(self.hass)
        ents = er.async_get(self.hass)
        changes: dict[str, Any] = {}
        if (area := choice.get("area_id")) is not None:
            changes["area_id"] = area or None
        if name := (choice.get("name") or "").strip():
            changes["name_by_user"] = name
        if changes:
            devices.async_update_device(device_id, **changes)
        for entity_id in choice.get("enable") or []:
            if any(d["entity_id"] == entity_id for d in sug["diagnostics"]):
                ents.async_update_entity(entity_id, disabled_by=None)
        for entity_id, cls in (choice.get("device_class") or {}).items():
            if entity_id in sug["entities"] and cls:
                ents.async_update_entity(entity_id, device_class=cls)
        if (
            sim := [e for e in choice.get("sim") or [] if e in sug["sim"]]
        ) and self.sim is not None:
            s = self.sim.settings()
            self.sim.set_settings(
                {
                    "lights": [*s["lights"], *[e for e in sim if e.startswith("light.")]],
                    "covers": [*s["covers"], *[e for e in sim if e.startswith("cover.")]],
                }
            )
        self.done(device_id)
        return sug

    @callback
    def done(self, device_id: str) -> None:
        """Done (or dismissed): no longer shown."""
        done = self.data.setdefault("done", [])
        if device_id not in done:
            done.append(device_id)
        del done[:-500]
        self._save()
