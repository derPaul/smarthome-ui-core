"""Sensors: one per task (when it is due), the last notification and the user of each display."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from . import CoreConfigEntry, durations
from .const import SIGNAL_MOWER, SIGNAL_SENT, SIGNAL_TASKS, SIGNAL_VACUUM, SIGNAL_WASTE
from .displays import DisplayEntity, async_setup_display_entities
from .entity import DEVICE_HOUSEHOLD, DEVICE_NOTIFY, CoreEntity, device_for_source
from .mower import MowerTracker
from .planner import STATUS, PlannerEntity, RobotPlanner, async_setup_planner_entities
from .store import CoreStore
from .tasks import TaskState, task_states
from .vacuum import VacuumTracker
from .waste import WasteManager

TASK_PREFIX = "task_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors; add and remove task sensors when the tasks change."""
    store = entry.runtime_data.store
    async_add_entities([LastNotificationSensor(entry.entry_id, store)])
    manager = entry.runtime_data.displays
    entry.async_on_unload(
        async_setup_display_entities(
            hass,
            entry.entry_id,
            manager,
            async_add_entities,
            lambda did: [DisplayUser(entry.entry_id, manager, did, "user", "sensor")],
        )
    )
    mowers = entry.runtime_data.mowers
    areas: set[tuple[str, str]] = set()

    @callback
    def add_areas(_mower: str | None = None) -> None:
        for tracker in mowers.trackers.values():
            new = [
                MowAreaSensor(entry.entry_id, tracker, name)
                for name in tracker.areas
                if (tracker.id, name.lower()) not in areas
            ]
            if not new:
                continue
            areas.update((tracker.id, s.area.lower()) for s in new)
            async_add_entities(new, config_subentry_id=tracker.id)

    add_areas()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_MOWER, add_areas))
    vacuums = entry.runtime_data.vacuums
    rooms: set[tuple[str, str]] = set()

    @callback
    def add_rooms(_vacuum: str | None = None) -> None:
        for tracker in vacuums.trackers.values():
            new = [
                VacuumRoomSensor(entry.entry_id, tracker, area)
                for area in tracker.rooms
                if (tracker.id, area) not in rooms
            ]
            if new:
                rooms.update((tracker.id, s.area_id) for s in new)
                async_add_entities(new, config_subentry_id=tracker.id)

    add_rooms()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_VACUUM, add_rooms))
    waste = entry.runtime_data.waste
    async_add_entities([WasteNextSensor(entry.entry_id, waste)])
    kinds: set[str] = set()

    @callback
    def add_kinds() -> None:
        new = [
            WasteKindSensor(entry.entry_id, waste, k["name"])
            for day in waste.days
            for k in day["kinds"]
            if k["name"] not in kinds and not kinds.add(k["name"])
        ]
        if new:
            async_add_entities(new)

    add_kinds()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_WASTE, add_kinds))
    entry.async_on_unload(
        async_setup_planner_entities(
            hass,
            entry.runtime_data.planners,
            async_add_entities,
            lambda p: [PlannerStatus(entry.entry_id, p)],
        )
    )
    known: dict[str, TaskSensor] = {}

    @callback
    def sync() -> None:
        tasks = {slugify(task["key"]): task for task in store.data["tasks"]}
        keys = {slug: task["key"] for slug, task in tasks.items()}
        new = [
            TaskSensor(entry.entry_id, store, task["key"], slug, device_for_source(task.get("src")))
            for slug, task in tasks.items()
            if slug not in known
        ]
        for sensor in new:
            known[sensor.slug] = sensor
        if new:
            async_add_entities(new)
        registry = er.async_get(hass)
        prefix = f"{entry.entry_id}_{TASK_PREFIX}"
        for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
            if (
                entity.unique_id.startswith(prefix)
                and entity.unique_id.removeprefix(prefix) not in keys
            ):
                known.pop(entity.unique_id.removeprefix(prefix), None)
                registry.async_remove(entity.entity_id)

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_TASKS, sync))


class TaskSensor(CoreEntity, SensorEntity):
    """When a task is due next."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "task"

    def __init__(self, entry_id: str, store: CoreStore, key: str, slug: str, device: str) -> None:
        """Initialize the sensor."""
        name = next((t.get("name") for t in store.data["tasks"] if t["key"] == key), None)
        super().__init__(
            entry_id, f"{TASK_PREFIX}{slug}", device, "sensor", slugify(name or "") or slug
        )
        self._store = store
        self._key = key
        self.slug = slug

    @property
    def _task(self) -> TaskState | None:
        task = next((t for t in self._store.data["tasks"] if t["key"] == self._key), None)
        if task is None:
            return None
        return task_states([task], self._store.done)[0]

    @property
    def name(self) -> str | None:
        """Return the task name."""
        task = self._task
        return task.name if task else self._key

    @property
    def icon(self) -> str | None:
        """Return the task icon."""
        task = self._task
        return (task.icon if task else None) or "mdi:calendar-check-outline"

    @property
    def native_value(self) -> datetime | None:
        """Return when the task is due."""
        task = self._task
        return task.due_datetime if task else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details of the task."""
        task = self._task
        if task is None:
            return {}
        today = dt_util.now().date()
        days = None if task.due_date is None else (task.due_date - today).days
        return {
            "key": task.key,
            "source": task.src,
            "area": task.area,
            "interval_days": task.interval,
            "last_done": task.last_datetime.isoformat() if task.last_datetime else None,
            "done_by": task.by,
            "done_who": task.who,
            "days_left": days,
            "due": days is not None and days <= 0,
        }

    async def async_added_to_hass(self) -> None:
        """Update when tasks or done records change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_TASKS, self.async_write_ha_state)
        )


class LastNotificationSensor(CoreEntity, SensorEntity):
    """When the last notification was sent, with the recent ones as attribute."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "last_notification"
    _unrecorded_attributes = frozenset({"recent"})

    def __init__(self, entry_id: str, store: CoreStore) -> None:
        """Initialize the sensor."""
        super().__init__(entry_id, "last_notification", DEVICE_NOTIFY, "sensor", "last_sent")
        self._store = store

    @property
    def native_value(self) -> datetime | None:
        """Return when the last notification was sent."""
        recent = self._store.data["recent"]
        return dt_util.utc_from_timestamp(recent[0]["t"] / 1000) if recent else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the recent notifications."""
        return {
            "recent": [
                {**item, "t": dt_util.utc_from_timestamp(item["t"] / 1000).isoformat()}
                for item in self._store.data["recent"]
            ]
        }

    async def async_added_to_hass(self) -> None:
        """Update after each notification."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SENT, self.async_write_ha_state)
        )


class DisplayUser(DisplayEntity, SensorEntity):
    """Who is logged in on a display."""

    @property
    def native_value(self) -> str | None:
        """Return the user name."""
        return self._record.get("user")


class MowAreaSensor(SensorEntity):
    """When a mowing area was last mowed; belongs to the mower's own device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "mow_area"

    def __init__(self, entry_id: str, tracker: MowerTracker, area: str) -> None:
        """Initialize the sensor (entity ID: sensor.garden_<area>_last_mowed)."""
        self._tracker = tracker
        self.area = area
        self._attr_unique_id = f"{entry_id}_mower_{tracker.id}_{slugify(area)}"
        self._attr_translation_placeholders = {"area": area}
        self.entity_id = f"sensor.garden_{slugify(area)}_last_mowed"
        if identifiers := tracker.device_identifiers():
            self._attr_device_info = DeviceInfo(identifiers=identifiers)

    @property
    def native_value(self) -> datetime | None:
        """Return when the area was last mowed."""
        last = self._tracker.area_record(self.area).get("last")
        return dt_util.utc_from_timestamp(last / 1000) if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return how it was mowed and whether it is being mowed now."""
        rec = self._tracker.area_record(self.area)
        due = self._tracker.due(self.area)
        days_left = None
        if due is not None:
            due_day = dt_util.as_local(dt_util.utc_from_timestamp(due / 1000)).date()
            days_left = (due_day - dt_util.now().date()).days
        return {
            "mower": self._tracker.name,
            "mode": rec.get("mode") or None,
            "minutes": rec.get("mins"),
            "mowing": self._tracker.mowing(self.area),
            "frequency": self._tracker.frequency(self.area),
            "interval_days": self._tracker.days(self.area) or None,
            "next_due": dt_util.utc_from_timestamp(due / 1000).isoformat() if due else None,
            "days_left": days_left,
            "due": days_left is not None and days_left <= 0,
            "usual_minutes": durations.usual(self._tracker.store, self._tracker.id, self.area),
        }

    @property
    def icon(self) -> str:
        """Return a mower while mowing."""
        return "mdi:robot-mower" if self._tracker.mowing(self.area) else "mdi:grass"

    async def async_added_to_hass(self) -> None:
        """Update when the mower changes."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_MOWER, self._changed))

    @callback
    def _changed(self, mower_id: str) -> None:
        if mower_id == self._tracker.id:
            self.async_write_ha_state()


class VacuumRoomSensor(SensorEntity):
    """When the robot last cleaned a room; belongs to the vacuum's own device."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_translation_key = "vacuum_room"
    _attr_icon = "mdi:robot-vacuum"

    def __init__(self, entry_id: str, tracker: VacuumTracker, area_id: str) -> None:
        """Initialize the sensor (entity ID: sensor.household_<room>_last_vacuumed)."""
        self._tracker = tracker
        self.area_id = area_id
        name = tracker.area_name(area_id)
        self._attr_unique_id = f"{entry_id}_vacuum_{tracker.id}_{area_id}"
        self._attr_translation_placeholders = {"area": name}
        self.entity_id = f"sensor.household_{slugify(name) or area_id}_last_vacuumed"
        if identifiers := tracker.device_identifiers():
            self._attr_device_info = DeviceInfo(identifiers=identifiers)

    @property
    def native_value(self) -> datetime | None:
        """Return when the room was last cleaned by the robot."""
        last = self._tracker.last_cleaned(self.area_id)
        return dt_util.utc_from_timestamp(last / 1000) if last else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the area and vacuum."""
        store, vac, area = self._tracker.store, self._tracker.id, self.area_id
        last_runs = durations.history(store, vac, area)
        mopped = self._tracker.last_mopped(area)
        return {
            "area_id": area,
            "vacuum": self._tracker.name,
            "last_minutes": last_runs[-1] if last_runs else None,
            "usual_minutes": durations.usual(store, vac, area),
            "usual_minutes_mopping": durations.usual(store, vac, f"{area}|mop"),
            "last_mopped": dt_util.utc_from_timestamp(mopped / 1000).isoformat()
            if mopped
            else None,
        }

    async def async_added_to_hass(self) -> None:
        """Update when the vacuum finished a run."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_VACUUM, self._changed))

    @callback
    def _changed(self, vacuum_id: str) -> None:
        if vacuum_id == self._tracker.id:
            self.async_write_ha_state()


class PlannerStatus(PlannerEntity, SensorEntity):
    """What the robot planner is waiting for (or that it started)."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STATUS
    _attr_translation_key = "planner"

    def __init__(self, entry_id: str, planner: RobotPlanner) -> None:
        """Initialize (sensor.garden_<mower>_planner / sensor.household_<vacuum>_planner)."""
        super().__init__(
            entry_id, planner, "_status", "sensor", f"{slugify(planner.tracker.name)}_planner"
        )

    @property
    def native_value(self) -> str:
        """Return the status."""
        return self._planner.status

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the due areas and the planned start."""
        start = self._planner.start_at
        return {
            "due": [self._planner.area_name(a) for a in self._planner.due],
            "mop_due": [self._planner.area_name(a) for a in self._planner.mop_due],
            "unreachable": [self._planner.area_name(a) for a in self._planner.unreachable],
            "start_at": dt_util.utc_from_timestamp(start / 1000).isoformat() if start else None,
        }


class WasteNextSensor(CoreEntity, SensorEntity):
    """The next waste collection (date) with what is picked up."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_translation_key = "waste_next"

    def __init__(self, entry_id: str, waste: WasteManager) -> None:
        """Initialize (sensor.household_waste_next)."""
        super().__init__(entry_id, "waste_next", DEVICE_HOUSEHOLD, "sensor", "waste_next")
        self._waste = waste

    @property
    def native_value(self) -> date | None:
        """Return the next pickup day."""
        return self._waste.days[0]["date"] if self._waste.days else None

    @property
    def icon(self) -> str:
        """Return the icon of the first kind."""
        return (
            self._waste.days[0]["kinds"][0]["icon"] if self._waste.days else "mdi:trash-can-outline"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the kinds, days until and whether it is time to put it out."""
        if not self._waste.days:
            return {}
        entry = self._waste.days[0]
        current = self._waste.current()
        return {
            "kinds": [k["name"] for k in entry["kinds"]],
            "days_until": (entry["date"] - dt_util.now().date()).days,
            "reminder": current is not None,
            "put_out": self._waste.done(entry["date"]),
            "upcoming": [
                {"date": d["date"].isoformat(), "kinds": [k["name"] for k in d["kinds"]]}
                for d in self._waste.days[:6]
            ],
        }

    async def async_added_to_hass(self) -> None:
        """Update when the calendars were read or something was put out."""
        for signal in (SIGNAL_WASTE, SIGNAL_TASKS):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self.async_write_ha_state)
            )


class WasteKindSensor(CoreEntity, SensorEntity):
    """When one kind of waste is picked up next."""

    _attr_device_class = SensorDeviceClass.DATE
    _attr_translation_key = "waste_kind"

    def __init__(self, entry_id: str, waste: WasteManager, kind: str) -> None:
        """Initialize (sensor.household_waste_<kind>)."""
        slug = slugify(kind)
        super().__init__(entry_id, f"waste_{slug}", DEVICE_HOUSEHOLD, "sensor", f"waste_{slug}")
        self._waste = waste
        self._kind = kind
        self._attr_translation_placeholders = {"kind": kind}

    def _next(self) -> dict[str, Any] | None:
        for entry in self._waste.days:
            for kind in entry["kinds"]:
                if kind["name"] == self._kind:
                    return {**kind, "date": entry["date"]}
        return None

    @property
    def native_value(self) -> date | None:
        """Return the next pickup day of this kind."""
        nxt = self._next()
        return nxt["date"] if nxt else None

    @property
    def icon(self) -> str:
        """Return the kind's icon."""
        nxt = self._next()
        return nxt["icon"] if nxt else "mdi:trash-can-outline"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the days until the pickup."""
        nxt = self._next()
        return {"days_until": (nxt["date"] - dt_util.now().date()).days} if nxt else {}

    async def async_added_to_hass(self) -> None:
        """Update when the calendars were read."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_WASTE, self.async_write_ha_state)
        )
