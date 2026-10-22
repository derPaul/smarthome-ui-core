"""Maintenance parts: carbon filters, air and water filters, backwash filter, smoke detectors …

A part is added as subentry ("Add maintenance part"): kind, area, optionally the device it belongs
to (e.g. the bathroom fan). Without a device Core creates one ("Kohlefilter Bad") in the area.

Each part has up to two intervals in months (0 = off): clean (backwash filter: backwash, smoke
detector: test) and replace. Replacing counts as cleaned too. What was done when is kept in the
done records (``part:<id>|clean`` / ``part:<id>|replace``) like everything the dashboard checks
off; due parts are tasks (to-do list "Haushalt", notification at the task hour).

Maintenance tasks the dashboard had for such parts ("Kohlefilter tauschen" in Bad and Gäste-WC)
move here once: one part per kind and area, with the last done dates.
"""

from __future__ import annotations

from collections.abc import Callable
import re
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from .chores import add_every, interval_days, start_of_day
from .const import DOMAIN, SIGNAL_DATA
from .store import CoreStore, now_ms

SUBENTRY_PART = "part"
CONF_KIND = "kind"
CONF_AREA = "area_id"
CONF_DEVICE = "device_id"
CONF_SINCE = "since"
ACTIONS = ("clean", "replace")

# kind: label, icon, (clean months, verb), (replace months, verb), pattern of task names
KINDS: dict[str, dict[str, Any]] = {
    "carbon_filter": {
        "label": "Kohlefilter",
        "icon": "mdi:air-filter",
        "clean": (3, "clean"),
        "replace": (12, "replace"),
        "match": r"(aktiv)?kohle|carbon|charcoal",
    },
    "air_filter": {
        "label": "Luftfilter",
        "icon": "mdi:air-filter",
        "clean": (1, "clean"),
        "replace": (6, "replace"),
        "match": r"luftfilter|lüftungsfilter|lueftungsfilter|filtergitter|pollenfilter|air.?filter",
    },
    "hood_filter": {
        "label": "Dunstabzugsfilter",
        "icon": "mdi:fan",
        "clean": (1, "clean"),
        "replace": (0, "replace"),
        "match": r"dunstabzug|abzugshaube|fettfilter|hood",
    },
    "backwash_filter": {
        "label": "Rückspülfilter",
        "icon": "mdi:water-sync",
        "clean": (6, "backwash"),
        "replace": (0, "replace"),
        "match": r"rückspül|rueckspuel|ruckspul|druckminderer|hauswasserfilter|backwash",
    },
    "water_filter": {
        "label": "Wasserfilter",
        "icon": "mdi:water-outline",
        "clean": (0, "clean"),
        "replace": (6, "replace"),
        "match": r"wasserfilter|water.?filter|filterkartusche",
    },
    "smoke_detector": {
        "label": "Rauchmelder",
        "icon": "mdi:smoke-detector-outline",
        "clean": (12, "test"),
        "replace": (120, "replace"),
        "match": r"rauchmelder|rauchwarnmelder|smoke",
    },
    "other": {
        "label": "Wartungsteil",
        "icon": "mdi:wrench-clock",
        "clean": (3, "clean"),
        "replace": (0, "replace"),
        "match": None,
    },
}
KIND_OPTIONS = list(KINDS)
VERBS = {"clean": "reinigen", "backwash": "rückspülen", "test": "testen", "replace": "tauschen"}
DONE_VERBS = {
    "clean": "gereinigt",
    "backwash": "rückgespült",
    "test": "getestet",
    "replace": "getauscht",
}
_REPLACE = re.compile(r"(tausch|wechsel|ersetz|erneuer|austausch|replace|change)", re.IGNORECASE)


def detect_kind(text: str) -> str | None:
    """Return the kind of a maintenance task name (None if it is no part)."""
    low = text.lower()
    for kind, spec in KINDS.items():
        if spec["match"] and re.search(spec["match"], low):
            return kind
    return None


def default_months(kind: str, action: str) -> int:
    """Return the default interval of an action (months, 0 = off)."""
    return int(KINDS.get(kind, KINDS["other"])[action][0])


def _months(every: dict[str, Any]) -> int:
    days = interval_days(every) or 0
    return max(1, round(days / 30.44))


class Part:
    """One configured maintenance part."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, subentry: ConfigSubentry) -> None:
        """Initialize the part."""
        self.hass = hass
        self.store = store
        self.id = subentry.subentry_id
        self.name = subentry.title
        data = subentry.data
        self.kind: str = data.get(CONF_KIND) if data.get(CONF_KIND) in KINDS else "other"
        self.area_id: str | None = data.get(CONF_AREA) or None
        self.device_id: str | None = data.get(CONF_DEVICE) or None
        self.since: int = int(data.get(CONF_SINCE) or now_ms())
        self.every = {
            action: int(
                data.get(f"{action}_every")
                if data.get(f"{action}_every") is not None
                else default_months(self.kind, action)
            )
            for action in ACTIONS
        }
        self.own_device: tuple[str, str] | None = None

    @property
    def spec(self) -> dict[str, Any]:
        """Return the kind's defaults."""
        return KINDS[self.kind]

    @property
    def actions(self) -> list[str]:
        """Return the actions with an interval."""
        return [a for a in ACTIONS if self.every[a] > 0]

    def verb_key(self, action: str) -> str:
        """Return clean / backwash / test / replace."""
        return self.spec[action][1]

    def verb(self, action: str) -> str:
        """Return the German verb (reinigen, rückspülen, testen, tauschen)."""
        return VERBS[self.verb_key(action)]

    def key(self, action: str) -> str:
        """Return the done key of an action."""
        return f"part:{self.id}|{action}"

    def area_name(self) -> str | None:
        """Return the name of the area."""
        area = ar.async_get(self.hass).async_get_area(self.area_id) if self.area_id else None
        return area.name if area else None

    def _done(self, action: str) -> int | None:
        return int((self.store.done.get(self.key(action)) or {}).get("t") or 0) or None

    def last(self, action: str) -> int | None:
        """Return when it was last done (replacing counts as cleaned)."""
        if action == "clean":
            return max(self._done("clean") or 0, self._done("replace") or 0) or None
        return self._done(action)

    def due(self, action: str) -> int | None:
        """Return when it is due next (from the last time or from when it was added)."""
        months = self.every[action]
        if months <= 0:
            return None
        base = self.last(action) or self.since
        return start_of_day(add_every(base, {"n": months, "unit": "m"}))

    def device_info(self) -> DeviceInfo | None:
        """Return the device our entities belong to (the linked one or our own)."""
        if self.device_id:
            device = dr.async_get(self.hass).async_get(self.device_id)
            if device is not None:
                return DeviceInfo(identifiers=set(device.identifiers))
        if self.own_device:
            return DeviceInfo(identifiers={self.own_device})
        return None

    def tasks(self) -> list[dict[str, Any]]:
        """Return "<part> reinigen" / "<part> tauschen" with their due dates."""
        return [
            {
                "key": self.key(action),
                "name": f"{self.name} {self.verb(action)}",
                "src": "hh.tasks",
                "area": self.area_name(),
                "icon": self.spec["icon"],
                "interval": interval_days({"n": self.every[action], "unit": "m"}),
                "last": self.last(action),
                "due": self.due(action),
            }
            for action in self.actions
        ]

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        device = self.device_id or None
        if device is None and self.own_device:
            entry = dr.async_get(self.hass).async_get_device(identifiers={self.own_device})
            device = entry.id if entry else None
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "icon": self.spec["icon"],
            "area": self.area_id,
            "device_id": device,
            "actions": {
                action: {
                    "key": self.key(action),
                    "every": self.every[action],
                    "verb": self.verb(action),
                    "done_verb": DONE_VERBS[self.verb_key(action)],
                    "last": self.last(action),
                    "due": self.due(action),
                }
                for action in self.actions
            },
        }

    @callback
    def mark(self, action: str, who: str | None = None) -> None:
        """Mark an action as done now."""
        verb = DONE_VERBS[self.verb_key(action)]
        self.store.mark_done(self.key(action), who=who, label=f"{self.name} {verb}")


class PartManager:
    """All maintenance parts of the config entry."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self.parts: dict[str, Part] = {}

    @callback
    def async_setup(self, entry: ConfigEntry) -> None:
        """Create the parts (and an own device for parts without one)."""
        devices = dr.async_get(self.hass)
        for subentry in entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_PART:
                continue
            part = Part(self.hass, self.store, subentry)
            self.parts[part.id] = part
            if part.device_id and devices.async_get(part.device_id):
                continue
            part.own_device = (DOMAIN, f"{entry.entry_id}_part_{part.id}")
            device = devices.async_get_or_create(
                config_entry_id=entry.entry_id,
                config_subentry_id=part.id,
                identifiers={part.own_device},
                name=part.name,
                manufacturer="Smarthome UI",
                model=part.spec["label"],
            )
            if part.area_id and device.area_id is None:
                devices.async_update_device(device.id, area_id=part.area_id)

    def info(self) -> list[dict[str, Any]]:
        """Return the parts for the dashboard."""
        return [p.info() for p in self.parts.values()]

    def tasks(self) -> list[dict[str, Any]]:
        """Return the parts' tasks (all, also those not due yet)."""
        return [task for p in self.parts.values() for task in p.tasks()]


@callback
def async_migrate_tasks(hass: HomeAssistant, entry: ConfigEntry, store: CoreStore) -> int:
    """Move dashboard maintenance tasks for parts into part subentries (once per task)."""
    defs = store.data.get("task_defs") or {}
    migrated: list[str] = store.data.setdefault("parts_migrated", [])
    done = store.done
    legacy = (store.data["data"].get("household") or {}).get("maint") or {}
    groups: dict[tuple[str, str | None], dict[str, Any]] = {}
    for task in defs.get("tasks") or []:
        if (task.get("scope") or "hh") != "hh" or task.get("windows") or not task.get("every"):
            continue
        if task["id"] in migrated or (kind := detect_kind(task.get("name") or "")) is None:
            continue
        action = "replace" if _REPLACE.search(task["name"]) else "clean"
        migrated.append(task["id"])
        for area in task.get("areas") or [None]:
            group = groups.setdefault((kind, area), {})
            group[f"{action}_every"] = _months(task["every"])
            record = done.get(f"hh:task:{task['id']}|{area or ''}")
            last = (record or {}).get("t") or legacy.get(f"{task['id']}|{area or ''}")
            if last:
                group[f"last_{action}"] = int(last)
    if not groups:
        if migrated:
            store.save()
        return 0
    areas = ar.async_get(hass)
    existing = {
        (sub.data.get(CONF_KIND), sub.data.get(CONF_AREA) or None): sub.subentry_id
        for sub in entry.subentries.values()
        if sub.subentry_type == SUBENTRY_PART
    }
    added = 0
    now = now_ms()
    for (kind, area), group in groups.items():
        part_id = existing.get((kind, area))
        if part_id is None:
            area_entry = areas.async_get_area(area) if area else None
            label = KINDS[kind]["label"]
            subentry = ConfigSubentry(
                data=MappingProxyType(
                    {
                        CONF_KIND: kind,
                        CONF_AREA: area,
                        CONF_SINCE: now,
                        "clean_every": group.get("clean_every", 0),
                        "replace_every": group.get("replace_every", 0),
                    }
                ),
                subentry_type=SUBENTRY_PART,
                title=f"{label} {area_entry.name}" if area_entry else label,
                unique_id=None,
            )
            hass.config_entries.async_add_subentry(entry, subentry)
            part_id = subentry.subentry_id
            added += 1
        for action in ACTIONS:
            key = f"part:{part_id}|{action}"
            if (last := group.get(f"last_{action}")) and not (done.get(key) or {}).get("t"):
                done[key] = {"t": last, "by": "hand"}
    store.save(SIGNAL_DATA)
    return added


class PartEntity(Entity):
    """An entity of a maintenance part (on the linked device or the part's own device)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, part: Part, action: str, key: str, domain: str) -> None:
        """Initialize (entity ID: <domain>.household_<part>_<key>)."""
        self._part = part
        self._action = action
        self._attr_unique_id = f"{entry_id}_part_{part.id}_{key}"
        self.entity_id = f"{domain}.household_{slugify(part.name)}_{key}"
        if info := part.device_info():
            self._attr_device_info = info

    async def async_added_to_hass(self) -> None:
        """Update when something is checked off (and once a day via the sensor's day)."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_DATA, self.async_write_ha_state)
        )


@callback
def async_setup_part_entities(
    manager: PartManager,
    async_add_entities: AddConfigEntryEntitiesCallback,
    create: Callable[[Part, str], Entity],
) -> None:
    """Add one entity per part and action."""
    for part in manager.parts.values():
        if entities := [create(part, action) for action in part.actions]:
            async_add_entities(entities, config_subentry_id=part.id)


def days_left(due: int | None) -> int | None:
    """Return the days until a due time (negative = overdue)."""
    if due is None:
        return None
    day = dt_util.as_local(dt_util.utc_from_timestamp(due / 1000)).date()
    return (day - dt_util.now().date()).days
