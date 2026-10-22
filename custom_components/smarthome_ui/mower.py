"""Robot mowers: track which area was mowed when, and notify about problems.

A mower is added as subentry ("Add robot mower") by picking its device. Everything else is found
on the device the way the dashboard does it (Mammotion, Husqvarna, Gardena …):

* the ``lawn_mower`` entity (state ``mowing`` / ``paused`` / ``returning`` / ``error`` …),
* area switches (``area`` / ``area_plain``) – their names are the known areas,
* the position sensor (``work_area``) and per-area sensors that appear during a job (their value is
  the mowing mode, e.g. ``border``),
* self check, blade usage and blade warning time for notifications.

The result goes to ``mow_log`` (same shape the dashboard used, plus the ``mower`` subentry id):
``{area: {"last": ms, "mode": str, "mins": int, "start": ms | None, "task": bool, "mower": id}}``.
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from datetime import date
import re
from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from . import durations
from .const import MOW_MIN_MINUTES, SIGNAL_DATA, SIGNAL_MOWER, SIGNAL_TASKS, TICK_INTERVAL
from .store import CoreStore, now_ms

SUBENTRY_MOWER = "mower"
CONF_DEVICE = "device_id"

INVALID = ("unknown", "unavailable", "")
_PREFIX = re.compile(r"^(bereich|area|zone|fläche)\s+", re.IGNORECASE)
_SKIP = re.compile(
    r"(^|_)(battery|akku|progress|fortschritt|left|rest|elapsed|total|speed|rssi|signal|error|"
    r"fehler|time|zeit|stars|satell|height|höhe|mode|modus|status|area$|fläche$|work_area|"
    r"position|ort$|wifi|cycles|distance)",
    re.IGNORECASE,
)
_MODE_VALUE = re.compile(
    r"^(mow|mowing|border|edge|grid|zone|working|chessboard|spot|kanten|rand|mähen)", re.IGNORECASE
)
_NO_ZONE = re.compile(r"not working|keine|none|unknown|unavailable", re.IGNORECASE)
_MODES = {
    "mowing": "Mähen",
    "mow": "Mähen",
    "working": "Mähen",
    "border": "Kanten",
    "edge": "Kanten",
    "edges": "Kanten",
    "perimeter": "Kanten",
    "along_border": "Kanten",
    "grid": "Raster",
    "chessboard": "Schachbrett",
    "zone": "Bereich",
    "spot": "Punkt",
    "paused": "Pause",
    "returning": "Rückfahrt",
}
DAY_MS = 86_400_000
# days between two mowings per month (lawn in Central Europe; 0 = winter break)
DEFAULT_PLAN = {1: 0, 2: 0, 3: 5, 4: 4, 5: 2, 6: 2, 7: 3, 8: 3, 9: 4, 10: 5, 11: 0, 12: 0}
FREQ_FACTOR = {"often": 0.67, "normal": 1.0, "rare": 1.5}
FREQ_OPTIONS = ["often", "normal", "rare", "pause"]
CHECK_OK = ["ok", "OK", "unknown", "unavailable", "", "none", "normal", "rain"]
CHECK_OK += ["non_working_hours", "low_battery"]


def _is_number(value: str) -> bool:
    try:
        float(value)
    except (TypeError, ValueError):
        return False
    return True


def mode_text(value: str) -> str:
    """Return a readable mowing mode (``border`` → ``Kanten``)."""
    key = re.sub(r"^[a-z]+\.", "", str(value or "").lower())
    return _MODES.get(key, str(value or "").replace("_", " "))


def area_name(mower: str, full: str | None) -> str:
    """Return an area name without the mower name and "Bereich" in front."""
    text = str(full or "")
    if text.lower().startswith(mower.lower()):
        text = text[len(mower) :]
    text = _PREFIX.sub("", text.strip()).strip()
    return text or str(full or "")


class MowerTracker:
    """One configured mower."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, subentry: ConfigSubentry) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.store = store
        self.id = subentry.subentry_id
        self.name = subentry.title
        self.device_id: str = subentry.data[CONF_DEVICE]
        self._seen: dict[str, int] = {}
        self.active: dict[str, dict[str, Any]] = {}
        self.areas: list[str] = []
        self.on_unusual: Callable[[Any, str, dict[str, Any]], None] | None = None

    # ---------------------------------------------------------------- entities of the device

    def entities(self) -> list[er.RegistryEntry]:
        """Return the entities of the mower device."""
        return er.async_entries_for_device(er.async_get(self.hass), self.device_id)

    def _find(self, domain: str, keys: tuple[str, ...], pattern: str | None = None) -> str | None:
        entries = [e for e in self.entities() if e.domain == domain]
        hit = next((e for e in entries if e.translation_key in keys), None)
        if hit is None and pattern:
            regex = re.compile(pattern, re.IGNORECASE)
            for entry in entries:
                state = self.hass.states.get(entry.entity_id)
                text = f"{entry.translation_key or ''} {entry.entity_id.split('.')[1]} "
                text += state.attributes.get("friendly_name", "") if state else ""
                if regex.search(text):
                    hit = entry
                    break
        return hit.entity_id if hit else None

    @property
    def entity_id(self) -> str | None:
        """Return the lawn_mower entity."""
        return next((e.entity_id for e in self.entities() if e.domain == "lawn_mower"), None)

    @property
    def zone_entity(self) -> str | None:
        """Return the position sensor (current work area)."""
        return self._find(
            "sensor",
            ("work_area",),
            r"(work_area|current location|aktueller ort|aktuelle position|standort)",
        )

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs to show this mower (with its mowing plan)."""
        settings = self.settings()
        return {
            "id": self.id,
            "name": self.name,
            "device_id": self.device_id,
            "entity": self.entity_id,
            "plan": dict(settings.get("plan") or {}),
            "interval": settings.get("interval"),
            "freq": dict(settings.get("freq") or {}),
            "configured": bool(settings),
        }

    # ---------------------------------------------------------------- mowing plan and due areas

    def settings(self) -> dict[str, Any]:
        """Return the own plan, fixed interval and frequency per area."""
        return self.store.data["mowing"].get(self.id) or {}

    @callback
    def update_settings(self, changes: dict[str, Any]) -> None:
        """Change plan (own values per month), fixed interval or frequency per area."""
        settings = deepcopy(self.settings())
        if "plan" in changes:
            settings["plan"] = {
                str(int(k)): max(0, int(v))
                for k, v in (changes["plan"] or {}).items()
                if 1 <= int(k) <= 12
            }
        if "interval" in changes:
            settings["interval"] = int(changes["interval"]) if changes["interval"] else None
        if "freq" in changes:
            settings["freq"] = {
                str(a): v for a, v in (changes["freq"] or {}).items() if v in FREQ_OPTIONS
            }
        self.store.data["mowing"][self.id] = settings
        self.store.save(SIGNAL_DATA, SIGNAL_TASKS)
        async_dispatcher_send(self.hass, SIGNAL_MOWER, self.id)

    @callback
    def set_frequency(self, area: str, value: str) -> None:
        """Change how often one area is mowed."""
        freq = dict(self.settings().get("freq") or {})
        if value == "normal":
            freq.pop(area, None)
        else:
            freq[area] = value
        self.update_settings({"freq": freq})

    def frequency(self, area: str) -> str:
        """Return often / normal / rare / pause."""
        return (self.settings().get("freq") or {}).get(area, "normal")

    def days(self, area: str, day: date | None = None) -> int:
        """Return the days between two mowings of an area (0 = not now)."""
        day = day or dt_util.now().date()
        freq = self.frequency(area)
        own = self.settings().get("plan") or {}
        base = int(own.get(str(day.month), DEFAULT_PLAN[day.month]))
        if base <= 0 or freq == "pause":
            return 0
        if interval := self.settings().get("interval"):
            return int(interval)
        return max(1, round(base * FREQ_FACTOR.get(freq, 1.0)))

    def last_mowed(self, area: str) -> int | None:
        """Return when an area was last mowed (a short pass without a job does not count)."""
        rec = self.area_record(area)
        last = rec.get("last")
        if last and not rec.get("mode") and (rec.get("mins") or 0) < MOW_MIN_MINUTES:
            return None
        return last or None

    def due(self, area: str) -> int | None:
        """Return when an area is due next (ms), None in a break or if never mowed."""
        last, days = self.last_mowed(area), self.days(area)
        return last + days * DAY_MS if last and days else None

    def task_key(self, area: str) -> str:
        """Return the task key of mowing an area."""
        return f"mow:{self.id}|{area}"

    def tasks(self) -> list[dict[str, Any]]:
        """Return "mow <area>" as tasks (garden to-do list and due notification)."""
        out = []
        for area in self.areas:
            if (due := self.due(area)) is None or self.mowing(area):
                continue
            out.append(
                {
                    "key": self.task_key(area),
                    "name": f"{area} mähen",
                    "src": "gd.mower",
                    "area": area,
                    "icon": "mdi:grass",
                    "interval": self.days(area),
                    "last": self.last_mowed(area),
                    "due": due,
                }
            )
        return out

    # ---------------------------------------------------------------- notifications

    def rules(self, sources: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the notification rules for this mower (if push is on for "Mähroboter")."""
        cfg = sources.get("gd.mower") or {}
        if not cfg.get("push") or not (mower := self.entity_id):
            return []
        base = {
            "src": "gd.mower",
            "title": "Mähroboter",
            "urgent": bool(cfg.get("urgent")),
            "url": cfg.get("url") or "/",
        }
        rules = [
            {
                **base,
                "id": f"mower|{self.id}|error",
                "type": "state",
                "entity": mower,
                "to": ["error"],
                "message": f"{self.name}: Fehler",
            }
        ]
        check = self._find("sensor", ("self_check",), r"(self.?check|selbsttest)")
        if check:
            rules.append(
                {
                    **base,
                    "id": f"mower|{self.id}|check",
                    "type": "template",
                    "template": f"{{{{ states('{check}') not in {CHECK_OK} }}}}",
                    "for": 120,
                    "message": f"{self.name}: Selbsttest meldet ein Problem",
                }
            )
        used = self._find(
            "sensor", ("blade_used_time",), r"(blade used|klingen|messer.*(nutz|verwend|betrieb))"
        )
        warn = self._find(
            "sensor", ("blade_used_warn_time",), r"(blade.*warn|messer.*warn|warnzeit)"
        )
        if used and warn:
            rules.append(
                {
                    **base,
                    "id": f"mower|{self.id}|blade",
                    "type": "template",
                    "template": (
                        f"{{{{ states('{warn}') | float(0) > 0 and "
                        f"states('{used}') | float(0) >= states('{warn}') | float(0) }}}}"
                    ),
                    "message": f"{self.name}: Messer wechseln",
                }
            )
        return rules

    # ---------------------------------------------------------------- areas

    def _log(self) -> dict[str, dict[str, Any]]:
        return self.store.data["data"].setdefault("mow_log", {})

    def known_areas(self) -> list[str]:
        """Return the area switches and the areas mowed so far."""
        names: list[str] = []

        def add(name: str | None) -> None:
            if name and name.lower() not in (n.lower() for n in names):
                names.append(name)

        for entry in self.entities():
            if entry.domain == "switch" and entry.translation_key in ("area", "area_plain"):
                state = self.hass.states.get(entry.entity_id)
                add(area_name(self.name, state.attributes.get("friendly_name") if state else None))
        for name, rec in self._log().items():
            if rec.get("mower") in (None, self.id):
                add(name)
        return names

    def current(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """Return the areas being mowed right now (per-area sensor or position)."""
        out: dict[str, dict[str, Any]] = {}
        mower = self.hass.states.get(self.entity_id or "")
        if mower is None or mower.state not in ("mowing", "paused", "returning"):
            return out
        lower = {n.lower(): n for n in names}
        zone = self.zone_entity
        for entry in self.entities():
            if entry.domain != "sensor" or entry.entity_id == zone:
                continue
            state = self.hass.states.get(entry.entity_id)
            if state is None or state.state in INVALID or _is_number(state.state):
                continue
            name = area_name(self.name, state.attributes.get("friendly_name") or entry.entity_id)
            hit = lower.get(name.lower())
            if hit or (
                not entry.translation_key
                and not _SKIP.search(entry.entity_id.split(".")[1])
                and _MODE_VALUE.match(state.state)
            ):
                out[hit or name] = {"mode": mode_text(state.state), "entity": entry.entity_id}
        # position only while really mowing (not on the way there or back)
        position = self.hass.states.get(zone) if zone and mower.state == "mowing" else None
        if (
            position is not None
            and position.state
            and not _NO_ZONE.search(position.state)
            and not _is_number(position.state)
        ):
            name = area_name(self.name, position.state)
            hit = lower.get(name.lower()) or name
            out.setdefault(hit, {"mode": "", "entity": zone})["pos"] = True
        return out

    @callback
    def evaluate(self) -> None:
        """Compare the current areas with the log; note start and end of each area."""
        names = self.known_areas()
        active = self.current(names)
        for name in active:
            if name not in names:
                names.append(name)
        now = now_ms()
        log = self._log()
        nxt = deepcopy(log)
        changed = False
        for name, cur_area in active.items():
            self._seen[name] = now
            cur = nxt.get(name) or {}
            if not cur.get("start"):
                nxt[name] = {
                    **cur,
                    "start": now,
                    "mode": cur_area.get("mode") or "",
                    "task": not cur_area.get("pos") or bool(cur_area.get("mode")),
                    "mower": self.id,
                    "prev": {
                        "last": cur.get("last"),
                        "mode": cur.get("mode") or "",
                        "mins": cur.get("mins") or 0,
                    },
                }
                changed = True
            elif cur_area.get("mode") and (
                cur.get("mode") != cur_area["mode"] or not cur.get("task")
            ):
                nxt[name] = {**cur, "mode": cur_area["mode"], "task": True}
                changed = True
        for name, cur in list(nxt.items()):
            if not cur.get("start") or name in active or cur.get("mower") not in (None, self.id):
                continue
            seen = self._seen.get(name, 0)
            end = seen if seen > cur["start"] else now
            mins = round(max(0, end - cur["start"]) / 60000)
            if cur.get("task") or mins >= MOW_MIN_MINUTES:
                nxt[name] = {
                    "last": end,
                    "mode": cur.get("mode") or "",
                    "mins": mins,
                    "mower": self.id,
                }
                self._duration(name, mins)
            else:  # only passing through: keep the previous result
                nxt[name] = {**(cur.get("prev") or {}), "mower": self.id}
            changed = True
        areas_changed = names != self.areas
        self.areas = names
        active_changed = set(active) != set(self.active)
        self.active = active
        if changed:
            self.store.set_mow_log(nxt)
        if changed or areas_changed or active_changed:
            async_dispatcher_send(self.hass, SIGNAL_MOWER, self.id)
            async_dispatcher_send(self.hass, SIGNAL_TASKS)

    def _duration(self, area: str, minutes: int) -> None:
        """Remember how long the area took; warn if that was unusual."""
        result = durations.record(self.store, self.id, area, minutes, min_diff=5)
        async_dispatcher_send(self.hass, SIGNAL_MOWER, self.id)
        if result is not None and self.on_unusual is not None:
            self.on_unusual(self, area, result)

    @callback
    def mark_mowed(self, area: str) -> None:
        """Mark an area as mowed by hand now."""
        log = deepcopy(self._log())
        log[area] = {
            **log.get(area, {}),
            "last": now_ms(),
            "start": None,
            "mode": "von Hand",
            "mins": 0,
            "mower": self.id,
        }
        log[area].pop("prev", None)
        self.store.set_mow_log(log)
        async_dispatcher_send(self.hass, SIGNAL_TASKS)
        async_dispatcher_send(self.hass, SIGNAL_MOWER, self.id)

    def area_record(self, name: str) -> dict[str, Any]:
        """Return the log entry of an area."""
        return self._log().get(name) or {}

    def mowing(self, name: str) -> bool:
        """Return True if the area is being mowed (not just passed through)."""
        cur = self.active.get(name)
        if not cur:
            return False
        if cur.get("mode") or not cur.get("pos"):
            return True
        start = self.area_record(name).get("start")
        return bool(start) and now_ms() - start >= MOW_MIN_MINUTES * 60000


class MowerManager:
    """All configured mowers of the config entry."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self.trackers: dict[str, MowerTracker] = {}
        self._unsub: list[CALLBACK_TYPE] = []
        self.on_unusual: Callable[[Any, str, dict[str, Any]], None] | None = None
        self._day: date | None = None

    @callback
    def async_setup(self, subentries: list[ConfigSubentry]) -> None:
        """Start tracking the mowers."""
        for subentry in subentries:
            if subentry.subentry_type == SUBENTRY_MOWER:
                tracker = MowerTracker(self.hass, self.store, subentry)
                tracker.on_unusual = self.on_unusual
                self.trackers[subentry.subentry_id] = tracker
        if not self.trackers:
            return
        registry = er.async_get(self.hass)
        devices = {t.device_id: t for t in self.trackers.values()}

        @callback
        def is_mower_entity(data: EventStateChangedData) -> bool:
            entry = registry.async_get(data["entity_id"])
            return entry is not None and entry.device_id in devices

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            entry = registry.async_get(event.data["entity_id"])
            if entry is not None and (tracker := devices.get(entry.device_id or "")):
                tracker.evaluate()

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=is_mower_entity)
        )
        self._unsub.append(async_track_time_interval(self.hass, self._tick, TICK_INTERVAL))
        self._tick()

    @callback
    def _tick(self, _now: Any = None) -> None:
        for tracker in self.trackers.values():
            tracker.evaluate()
        # a new day: due dates and plan month move on
        if (today := dt_util.now().date()) != self._day:
            self._day = today
            for tracker in self.trackers.values():
                async_dispatcher_send(self.hass, SIGNAL_MOWER, tracker.id)
            async_dispatcher_send(self.hass, SIGNAL_TASKS)

    @callback
    def async_stop(self) -> None:
        """Stop tracking."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def rules(self) -> list[dict[str, Any]]:
        """Return the notification rules of all mowers."""
        sources = self.store.data["notify"].get("sources") or {}
        return [rule for t in self.trackers.values() for rule in t.rules(sources)]

    def info(self) -> list[dict[str, Any]]:
        """Return the mowers for the dashboard."""
        return [t.info() for t in self.trackers.values()]

    def tasks(self) -> list[dict[str, Any]]:
        """Return the due areas of all mowers as tasks."""
        return [task for t in self.trackers.values() for task in t.tasks()]

    @callback
    def complete(self, key: str) -> bool:
        """Mark the area of a "mow:" task as mowed by hand; False if it is not one."""
        mower_id, _, area = key.removeprefix("mow:").partition("|")
        if not key.startswith("mow:") or (tracker := self.trackers.get(mower_id)) is None:
            return False
        tracker.mark_mowed(area)
        return True


RuleProvider = Callable[[], list[dict[str, Any]]]
