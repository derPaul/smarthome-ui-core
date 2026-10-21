"""Sensors: one per task (when it is due) and the last notification."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util, slugify

from . import CoreConfigEntry
from .const import SIGNAL_SENT, SIGNAL_TASKS
from .entity import DEVICE_NOTIFY, CoreEntity, device_for_source
from .store import CoreStore
from .tasks import TaskState, task_states

TASK_PREFIX = "task_"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the sensors; add and remove task sensors when the tasks change."""
    store = entry.runtime_data.store
    async_add_entities([LastNotificationSensor(entry.entry_id, store)])
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
