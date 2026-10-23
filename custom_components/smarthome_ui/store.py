"""Persistent data of Smarthome UI Core.

The dashboard's "done" data keeps its shape so the dashboard can use it unchanged:
``done[key] = {"t": ms, "by": "hand"|"robot"|"skip", "who": name}`` (``t == 0`` means reopened),
``done_log`` is a list of ``{"k", "t", "who", "label"}``. Keys look like ``clean:<area>``,
``hh:task:<id>|<area>``, ``gd:task:<id>|<area>`` and ``waste:<YYYY-MM-DD>``.
"""

from __future__ import annotations

from copy import deepcopy
import time
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from . import chores
from .const import (
    DATA_KEYS,
    DONE_LOG_MAX,
    SIGNAL_DATA,
    SIGNAL_NOTIFY,
    SIGNAL_TASKS,
    STORAGE_KEY,
    STORAGE_VERSION,
)


def now_ms() -> int:
    """Return the current time in milliseconds (the dashboard's time format)."""
    return int(time.time() * 1000)


def _default() -> dict[str, Any]:
    return {
        "data": {"done": {}, "done_log": [], "household": {}, "mow_log": {}},
        "migrated": False,
        "tasks": [],
        "notify": {
            "enabled": True,
            "rules": [],
            "targets": [],
            "sources": {},
            "quiet": None,
            "task_hour": None,
        },
        "sent": {},
        "recent": [],
        "displays": {},
        # robot mowers: {mower_id: {"plan": {month: days}, "interval": days, "freq": {area: …}}}
        "mowing": {},
        # robot planner: {robot_id: {"on": bool, "off": [areas], "skip": day, "started": day}}
        "planner": {},
        # last durations per robot and area (minutes): {robot_id: {area: [..]}}
        "durations": {},
        # task definitions from the dashboard (Core computes the due dates)
        "task_defs": {"tasks": [], "rooms": [], "configured": False},
        # waste collection: {calendars, names, remind: {d, t}, configured}
        "waste": {
            "calendars": [],
            "names": {},
            "remind": {"d": 1, "t": "12:00"},
            "configured": False,
        },
        # household: own cleaning interval per area (days, 0 = not tracked)
        "cleaning": {"intervals": {}, "configured": False},
        # household appliances: {appliance_id: {"run": {..}, "last": {..}, "finished": ms, ...}}
        "appliances": {},
        # dashboard maintenance tasks moved to maintenance parts (task ids)
        "parts_migrated": [],
        # plants: {entity: {"watered": ms, "samples": [[ms, moisture]]}}, own settings, last rain
        "plants": {},
        "plant_settings": {},
        "plant_rain": 0,
        # security: openings / night settings from the dashboard, checks, the night log
        "security": {},
        # doorbells: {doorbell_id: {"rings": [ms], "mute": bool}}
        "doorbells": {},
        # alarm logic: settings from the dashboard and its state (guests, night walk, reason)
        "alarm": {},
        # mailbox: chosen sensors, deliveries and emptyings (ms)
        "mailbox": {},
        # presence simulation: settings from the dashboard
        "presence_sim": {},
        # grocery spending: "last month" sensor and the kept months
        "grocery": {},
        # house technology: sensors from the dashboard, own thresholds, pressure / base load samples
        "technik": {},
        # forecasts: battery power per hour (7 days), hot water start temperatures
        "forecast": {},
        # system and network: what the dashboard watches (internet, devices, status, services)
        "system": {},
        # the dashboard's device settings were taken over into the catalog (device subentries)
        "catalog_migrated": False,
        # later takeovers of the catalog (step 3: home, rooms, household, garden)
        "catalog_steps": [],
        # lists of the dashboard's pages (cameras, windows, locks …)
        "lists": {},
        # setup assistant: done (existing installations count as done)
        "onboarding": {},
    }


class CoreStore:
    """Load, change and save the data; tell listeners what changed."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the store."""
        self.hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self.data: dict[str, Any] = _default()

    async def async_load(self) -> None:
        """Load the stored data, filling in what older versions did not have."""
        stored = await self._store.async_load() or {}
        data = _default()
        for key, value in stored.items():
            if isinstance(data.get(key), dict) and isinstance(value, dict):
                data[key] = {**data[key], **value}
            else:
                data[key] = value
        self.data = data

    async def async_flush(self) -> None:
        """Write pending changes now (before unloading)."""
        await self._store.async_save(self.data)

    @callback
    def save(self, *signals: str) -> None:
        """Save soon and notify listeners of the given signals."""
        self._store.async_delay_save(lambda: self.data, 2)
        for signal in signals:
            async_dispatcher_send(self.hass, signal)

    # ---------------------------------------------------------------- dashboard data

    @property
    def done(self) -> dict[str, dict[str, Any]]:
        """Return the done records."""
        return self.data["data"]["done"]

    def dashboard_data(self) -> dict[str, Any]:
        """Return a copy of the data the dashboard reads."""
        return deepcopy(self.data["data"])

    @callback
    def import_data(self, data: dict[str, Any], force: bool = False) -> bool:
        """Take over the dashboard's data once; return False if already migrated."""
        if self.data["migrated"] and not force:
            return False
        for key in DATA_KEYS:
            if key in data and data[key] is not None:
                self.data["data"][key] = deepcopy(data[key])
        self.data["migrated"] = True
        self.save(SIGNAL_DATA, SIGNAL_TASKS)
        return True

    @callback
    def set_data(self, data: dict[str, Any]) -> None:
        """Replace whole data keys (household, mow_log …)."""
        for key in DATA_KEYS:
            if key in data and data[key] is not None:
                self.data["data"][key] = deepcopy(data[key])
        self.save(SIGNAL_DATA, SIGNAL_TASKS)

    @callback
    def apply_done(
        self,
        changes: dict[str, dict[str, Any] | None],
        log: list[dict[str, Any]] | None = None,
        unlog: list[str] | None = None,
    ) -> None:
        """Apply done changes like the dashboard does (None removes a record)."""
        done = self.done
        for key, record in changes.items():
            if record is None:
                done.pop(key, None)
            else:
                done[key] = dict(record)
        entries = self.data["data"]["done_log"]
        if unlog:
            drop = set(unlog)
            entries[:] = [e for e in entries if f"{e.get('k')}|{e.get('t')}" not in drop]
        if log:
            entries.extend(log)
            del entries[:-DONE_LOG_MAX]
        self.save(SIGNAL_DATA, SIGNAL_TASKS)

    @callback
    def mark_done(self, key: str, by: str = "hand", who: str | None = None, label: str = "") -> int:
        """Mark one key as done now and log it; return the timestamp."""
        ts = now_ms()
        record: dict[str, Any] = {"t": ts, "by": by}
        if who:
            record["who"] = who
        self.apply_done({key: record}, [{"k": key, "t": ts, "who": who or "", "label": label}])
        return ts

    @callback
    def reopen(self, key: str) -> None:
        """Mark a key as open again."""
        self.apply_done({key: {"t": 0}})

    @callback
    def set_cleaning_intervals(self, intervals: dict[str, int]) -> None:
        """Replace the own cleaning intervals per area."""
        self.data["cleaning"] = {
            "intervals": {str(a): max(0, int(v)) for a, v in intervals.items()},
            "configured": True,
        }
        # synced tasks take the new interval right away (the dashboard confirms with its next sync)
        for task in self.data["tasks"]:
            area = str(task.get("key", "")).removeprefix("clean:")
            if (
                task.get("key", "").startswith("clean:")
                and area in self.data["cleaning"]["intervals"]
            ):
                days = self.data["cleaning"]["intervals"][area]
                task["interval"] = days or task.get("interval")
                if days and task.get("last"):
                    task["due"] = int(task["last"] + days * 86_400_000)
        self.save(SIGNAL_DATA, SIGNAL_TASKS)

    @callback
    def set_mow_log(self, log: dict[str, Any]) -> None:
        """Replace the mowing log (tracked by the mowers)."""
        self.data["data"]["mow_log"] = log
        self.save(SIGNAL_DATA)

    @callback
    def area_mowed(self, area: str) -> None:
        """Mark a mowing area as mowed by hand now."""
        log = self.data["data"].setdefault("mow_log", {})
        log[area] = {
            **log.get(area, {}),
            "last": now_ms(),
            "start": None,
            "mode": "von Hand",
            "mins": 0,
        }
        self.save(SIGNAL_DATA)

    # ---------------------------------------------------------------- tasks and notifications

    @property
    def own_tasks(self) -> bool:
        """Return True if Core computes the tasks from the definitions (dashboard sync ignored)."""
        return bool(self.data["task_defs"].get("configured"))

    @callback
    def set_task_defs(self, tasks: list[dict[str, Any]], rooms: list[dict[str, Any]]) -> None:
        """Take the task definitions from the dashboard and compute the tasks."""
        self.data["task_defs"] = {"tasks": tasks, "rooms": rooms, "configured": True}
        self.recompute_tasks(force=True)

    @callback
    def recompute_tasks(self, force: bool = False) -> None:
        """Compute the tasks from the definitions (only if Core owns them)."""
        if not self.own_tasks:
            return
        tasks = chores.compute(self.data, now_ms())
        if force or tasks != self.data["tasks"]:
            self.set_tasks(tasks)

    @callback
    def set_tasks(self, tasks: list[dict[str, Any]]) -> None:
        """Store the task definitions the dashboard sends."""
        self.data["tasks"] = [dict(task) for task in tasks if task.get("key") and task.get("name")]
        self.save(SIGNAL_TASKS)

    @callback
    def set_notify(self, config: dict[str, Any]) -> None:
        """Store notification settings and rules."""
        self.data["notify"].update(config)
        self.save(SIGNAL_NOTIFY)
