"""Robot vacuums: track which room was cleaned when, and notify about problems and wear.

A vacuum is added as subentry ("Add robot vacuum") by picking its device. Core finds on the device
(and a station device linked to it) the ``vacuum`` entity, the current-room sensor (Roborock …),
error sensors, the cleaned-area sensor and the consumables (time left of brushes, filter …).

While the vacuum is cleaning, the time per room is added up; a room counts as cleaned after
``VAC_MIN_MINUTES`` there. Stops at the station of up to 45 minutes (washing the mop, recharging)
belong to the same run. Without a room sensor a run started from the dashboard (``household.runs``)
or a full run (cleaned area ≥ 70 % of the largest so far) counts for its rooms.

The result goes to the dashboard's household data: ``household.cleaned[area].r`` (ms).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
import json
import re
from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send

from . import durations
from .const import SIGNAL_TASKS, SIGNAL_VACUUM, VAC_MIN_MINUTES
from .entity import all_devices
from .store import CoreStore, now_ms

SUBENTRY_VACUUM = "vacuum"
CONF_DEVICE = "device_id"

INVALID = ("unknown", "unavailable", "", "none")
NO_ERROR = ("none", "no_error", "ok", "unknown", "unavailable", "", "0", "kein fehler", "keiner")
SESSION_GAP_MS = 45 * 60_000
RUN_MATCH_MS = 5 * 60_000
# consumables: pattern, name, life in hours, what to do
CONSUMABLES = [
    (r"(main_brush|hauptbürste|hauptburste|main brush)", "Hauptbürste", 300, "reinigen/wechseln"),
    (r"(side_brush|seitenbürste|seitenburste|side brush)", "Seitenbürste", 200, "wechseln"),
    (r"(strainer|sieb)", "Sieb Station", 150, "reinigen"),
    (
        r"(maintenance_brush|wartungsbürste|wartungsburste|reinigungsbürste|maintenance brush)",
        "Bürste Station",
        300,
        "reinigen",
    ),
    (r"(filter)", "Filter", 150, "reinigen/wechseln"),
    (r"(sensor)", "Sensoren", 30, "reinigen"),
]
_HOURS = {"s": 1 / 3600, "min": 1 / 60, "h": 1, "d": 24}
# station sensors (binary, "on" = something to do)
STATION = [
    (
        r"(water_shortage|wassermangel|water shortage|clean_water|frischwasser|"
        r"water_box_empty|no_water)",
        "Frischwasser nachfüllen",
    ),
    (r"(dirty_water|waste_water|schmutzwasser|abwasser|sewage)", "Schmutzwasser leeren"),
    (r"(dust_bag|dustbag|staubbeutel|bag_full)", "Staubbeutel wechseln"),
]
# station sensors with a state (tank "empty" / "full", "problem"): key, bad value, what to do
_TANK_BAD = r"|problem|error|fehler|alarm|warning|warnung|abnormal|fault)"
STATION_SENSORS = [
    (
        r"(clean_water|frischwasser|water_tank|wassertank|water_box)",
        r"^(empty|leer|low|niedrig|insufficient|lack|shortage|not_enough|kein wasser|"
        r"nachfüllen|refill" + _TANK_BAD,
        "Frischwasser nachfüllen",
    ),
    (
        r"(dirty_water|waste_water|schmutzwasser|abwasser|sewage)",
        r"^(full|voll|leeren|empty_it|high" + _TANK_BAD,
        "Schmutzwasser leeren",
    ),
    (
        r"(dust_bag|dustbag|staubbeutel)",
        r"^(full|voll|wechseln|replace" + _TANK_BAD,
        "Staubbeutel wechseln",
    ),
]
_DIRTY = re.compile(r"(dirty|schmutz|waste|abwasser|sewage)")
_VAC = re.compile(r"(sweep|vacuum|saug)")
_MOP = re.compile(r"(mop|wisch|wipe)")
_MOP_LEVEL = re.compile(
    r"(mop_intensity|mop intensity|water_box|water box|wassermenge|wischintensit|scrub|"
    r"water_flow|water level|wasserstufe|wasserdurchfluss|mop_mode|wischmodus)"
)
_MOP_OFF = ("off", "aus", "closed", "close", "no_water", "none", "kein wasser")


def _norm(text: str | None) -> str:
    text = str(text or "").lower()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "", text)


class VacuumTracker:
    """One configured robot vacuum."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, subentry: ConfigSubentry) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.store = store
        self.id = subentry.subentry_id
        self.name = subentry.title
        self.device_id: str = subentry.data[CONF_DEVICE]
        self._session: dict[str, Any] | None = None
        self._cur: tuple[bool, str | None, int] | None = None
        self.rooms: list[str] = []
        self.on_unusual: Callable[[Any, str, dict[str, Any]], None] | None = None
        self._mop_level: str | None = None

    # ---------------------------------------------------------------- mopping

    def mop_control(self) -> dict[str, Any] | None:
        """Return how to switch between vacuuming only and vacuuming + mopping (None = no mop).

        Either a cleaning-mode select (e.g. Dreame: sweeping / sweeping_and_mopping) or a mop /
        water level select with an "off" option (e.g. Roborock mop intensity).
        """
        selects = []
        for entry in self.entities():
            if entry.domain != "select":
                continue
            state = self.hass.states.get(entry.entity_id)
            options = [str(o) for o in (state.attributes.get("options") or [])] if state else []
            if options:
                selects.append((entry, state, options))
        for entry, _state, options in selects:
            vac = [o for o in options if _VAC.search(o.lower()) and not _MOP.search(o.lower())]
            both = [o for o in options if _VAC.search(o.lower()) and _MOP.search(o.lower())]
            if vac and both:
                return {"entity": entry.entity_id, "vacuum": vac[0], "mop": both[0]}
        for entry, state, options in selects:
            if not _MOP_LEVEL.search(self._text(entry)):
                continue
            off = next((o for o in options if o.lower() in _MOP_OFF), None)
            if off is None:
                continue
            if state.state not in (off, *INVALID):
                self._mop_level = state.state
            on = self._mop_level or next(
                (o for o in options if o.lower() in ("medium", "moderate", "standard", "mittel")),
                next(o for o in options if o != off),
            )
            return {"entity": entry.entity_id, "vacuum": off, "mop": on}
        return None

    def is_mopping(self) -> bool:
        """Return True if the vacuum is set to mop right now."""
        control = self.mop_control()
        if control is None:
            return False
        state = self.hass.states.get(control["entity"])
        return state is not None and state.state not in (control["vacuum"], *INVALID)

    # ---------------------------------------------------------------- floors

    def floor_id(self) -> str | None:
        """Return the floor the vacuum is on right now.

        1. a map select of the vacuum whose current map is named like a floor (Roborock: one map per
           floor), 2. the area of the vacuum (entity or device) and its floor.
        """
        floors = fr.async_get(self.hass).async_list_floors()
        for entry in self.entities():
            if entry.domain != "select" or not re.search(
                r"(map|karte|floor|etage|stockwerk)", self._text(entry)
            ):
                continue
            state = self.hass.states.get(entry.entity_id)
            current = _norm(state.state) if state else ""
            if not current:
                continue
            for floor in floors:
                names = [
                    _norm(floor.name),
                    _norm(floor.floor_id),
                    *(_norm(a) for a in floor.aliases),
                ]
                if any(n and (n == current or n in current or current in n) for n in names):
                    return floor.floor_id
        entity = er.async_get(self.hass).async_get(self.entity_id or "")
        area_id = entity.area_id if entity else None
        if area_id is None:
            device = dr.async_get(self.hass).async_get(self.device_id)
            area_id = device.area_id if device else None
        return self.area_floor(area_id) if area_id else None

    def area_floor(self, area_id: str | None) -> str | None:
        """Return the floor of an area."""
        area = ar.async_get(self.hass).async_get_area(area_id) if area_id else None
        return area.floor_id if area else None

    def floor_name(self, floor_id: str | None) -> str:
        """Return the name of a floor."""
        floor = fr.async_get(self.hass).async_get_floor(floor_id) if floor_id else None
        return floor.name if floor else ""

    def reachable(self, area_id: str) -> bool:
        """Return True if the vacuum can get to the room (same floor, or a floor is unknown)."""
        here, there = self.floor_id(), self.area_floor(area_id)
        return here is None or there is None or here == there

    def last_mopped(self, area_id: str) -> int | None:
        """Return when the robot last mopped an area."""
        return ((self._household().get("cleaned") or {}).get(area_id) or {}).get("w") or None

    # ---------------------------------------------------------------- entities of the device

    def device_ids(self) -> list[str]:
        """Return the vacuum device and devices linked to it (e.g. the station)."""
        linked = [d.id for d in all_devices(self.hass) if d.via_device_id == self.device_id]
        return [self.device_id, *linked]

    def entities(self) -> list[er.RegistryEntry]:
        """Return the entities of the vacuum (and its station)."""
        registry = er.async_get(self.hass)
        return [e for d in self.device_ids() for e in er.async_entries_for_device(registry, d)]

    def _text(self, entry: er.RegistryEntry) -> str:
        state = self.hass.states.get(entry.entity_id)
        name = state.attributes.get("friendly_name", "") if state else ""
        return f"{entry.translation_key or ''} {entry.entity_id.split('.')[1]} {name}".lower()

    def _find(self, domain: str, pattern: str, exclude: str | None = None) -> str | None:
        regex, skip = re.compile(pattern), re.compile(exclude) if exclude else None
        for entry in self.entities():
            if entry.domain != domain:
                continue
            text = self._text(entry)
            if regex.search(text) and not (skip and skip.search(text)):
                return entry.entity_id
        return None

    @property
    def entity_id(self) -> str | None:
        """Return the vacuum entity."""
        return next((e.entity_id for e in self.entities() if e.domain == "vacuum"), None)

    @property
    def room_entity(self) -> str | None:
        """Return the current-room sensor (if the integration has one)."""
        return self._find("sensor", r"(current_room|aktueller.?raum|current room)")

    @property
    def area_entity(self) -> str | None:
        """Return the cleaned-area sensor of the current run."""
        return self._find(
            "sensor",
            r"(cleaning_area|reinigungsfläche|reinigungsflache|gereinigte fläche|cleaning area)",
            r"(total|gesamt)",
        )

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs to show this vacuum."""
        return {
            "id": self.id,
            "name": self.name,
            "device_id": self.device_id,
            "entity": self.entity_id,
        }

    # ---------------------------------------------------------------- rooms

    def _options(self) -> dict[str, Any]:
        entry = er.async_get(self.hass).async_get(self.entity_id or "")
        opts = entry.options.get("vacuum") if entry else None
        return dict(opts) if isinstance(opts, dict) else {}

    def mapped_areas(self) -> list[str]:
        """Return the areas assigned to the vacuum's segments in Home Assistant."""
        text = json.dumps(self._options())
        return [a.id for a in ar.async_get(self.hass).async_list_areas() if f'"{a.id}"' in text]

    def room_area(self, name: str | None) -> str | None:
        """Return the area of a room name of the vacuum (segment mapping, then area names)."""
        if not name:
            return None
        opts = self._options()
        seg_id: str | None = None

        def walk(value: Any) -> None:
            nonlocal seg_id
            if seg_id is not None or not isinstance(value, (dict, list)):
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if "name" in value and "id" in value and _norm(value["name"]) == _norm(name):
                seg_id = str(value["id"])
                return
            for item in value.values():
                walk(item)

        walk(opts)
        areas = ar.async_get(self.hass)
        if seg_id is not None:
            for mapping in opts.values():
                if isinstance(mapping, dict):
                    for area_id, ids in mapping.items():
                        if (
                            isinstance(ids, list)
                            and seg_id in map(str, ids)
                            and areas.async_get_area(area_id)
                        ):
                            return area_id
        wanted = _norm(name)
        candidates = [(a.id, [_norm(a.id), _norm(a.name)]) for a in areas.async_list_areas()]
        for area_id, names in candidates:
            if wanted in names:
                return area_id
        for area_id, names in candidates:
            if any(len(n) > 2 and (n in wanted or wanted in n) for n in names):
                return area_id
        return None

    def area_name(self, area_id: str) -> str:
        """Return the name of an area."""
        area = ar.async_get(self.hass).async_get_area(area_id)
        return area.name if area else area_id

    def _household(self) -> dict[str, Any]:
        return self.store.data["data"].setdefault("household", {})

    def last_cleaned(self, area_id: str) -> int | None:
        """Return when the robot last cleaned an area."""
        return ((self._household().get("cleaned") or {}).get(area_id) or {}).get("r") or None

    def known_rooms(self) -> list[str]:
        """Return the rooms of this vacuum: assigned segments and rooms cleaned so far."""
        rooms = list(self.mapped_areas())
        for area_id, rec in (self._household().get("cleaned") or {}).items():
            if rec.get("r") and area_id not in rooms and rec.get("vac") in (None, self.id):
                rooms.append(area_id)
        return rooms

    # ---------------------------------------------------------------- live tracking

    @callback
    def evaluate(self) -> None:
        """Add up the time per room while cleaning; note cleaned rooms when a run ends."""
        now = now_ms()
        vacuum = self.hass.states.get(self.entity_id or "")
        if vacuum is None:
            return
        cleaning = vacuum.state == "cleaning"
        room = self.hass.states.get(self.room_entity or "")
        area = (
            self.room_area(room.state) if room is not None and room.state not in INVALID else None
        )
        session = self._session
        # time since the last change counts for the room the vacuum was in
        if self._cur and session is not None:
            was_cleaning, was_area, since = self._cur
            if was_cleaning:
                session["clean_ms"] += now - since
                if was_area:
                    dwell = session["dwell"].setdefault(was_area, {"ms": 0, "last": 0})
                    dwell["ms"] += now - since
                    dwell["last"] = now
        self._cur = (cleaning, area, now)
        if cleaning:
            if session is None or (session.get("end") and now - session["end"] > SESSION_GAP_MS):
                self._session = {
                    "from": now,
                    "end": None,
                    "dwell": {},
                    "clean_ms": 0,
                    "max_area": 0,
                    "mop": self.is_mopping(),
                }
            elif session.get("end"):
                session["end"] = None
                session["mop"] = session.get("mop") or self.is_mopping()
        elif (
            session is not None
            and session.get("end") is None
            and vacuum.state
            in (
                "docked",
                "idle",
                "charging",
            )
        ):
            session["end"] = now
            self._finish(session)
        if self._session is not None and (sensor := self.hass.states.get(self.area_entity or "")):
            try:
                self._session["max_area"] = max(self._session["max_area"], float(sensor.state))
            except ValueError:
                pass
        rooms = self.known_rooms()
        if rooms != self.rooms:
            self.rooms = rooms
            async_dispatcher_send(self.hass, SIGNAL_VACUUM, self.id)

    def _finish(self, session: dict[str, Any]) -> None:
        """A run ended: write the cleaned rooms."""
        hh = self._household()
        min_ms = VAC_MIN_MINUTES * 60_000
        done = {a: d["last"] for a, d in session["dwell"].items() if d["ms"] >= min_ms}
        if not done and session["clean_ms"] > 3 * 60_000 and not session["dwell"]:
            # no room information: a run started from the dashboard, or a full run
            run = next(
                (
                    r
                    for r in reversed(hh.get("runs") or [])
                    if abs(r.get("t", 0) - session["from"]) < RUN_MATCH_MS
                ),
                None,
            )
            if run:
                done = dict.fromkeys(run.get("areas") or [], session["end"])
            else:
                full = float(hh.get("full_area") or 0)
                if not self.area_entity or not full or session["max_area"] >= full * 0.7:
                    done = dict.fromkeys(self.mapped_areas(), session["end"])
        cleaned = deepcopy(hh.get("cleaned") or {})
        for area_id, at in done.items():
            rec = cleaned.get(area_id) or {}
            cleaned[area_id] = {**rec, "r": max(rec.get("r") or 0, at), "vac": self.id}
            if session.get("mop"):
                cleaned[area_id]["w"] = max(rec.get("w") or 0, at)
        # usual time per room (vacuuming and mopping counted separately)
        for area_id, dwell in session["dwell"].items():
            minutes = round(dwell["ms"] / 60_000)
            if area_id not in done or minutes < VAC_MIN_MINUTES:
                continue
            key = f"{area_id}|mop" if session.get("mop") else area_id
            result = durations.record(self.store, self.id, key, minutes, min_diff=3)
            if result is not None and self.on_unusual is not None:
                self.on_unusual(self, area_id, result)
        changes: dict[str, Any] = {
            "cleaned": cleaned,
            "synced": max(hh.get("synced") or 0, session["end"]),
        }
        if session["max_area"] > float(hh.get("full_area") or 0):
            changes["full_area"] = session["max_area"]
        self.store.set_data({"household": {**hh, **changes}})
        self.rooms = self.known_rooms()
        async_dispatcher_send(self.hass, SIGNAL_VACUUM, self.id)
        async_dispatcher_send(self.hass, SIGNAL_TASKS)

    # ---------------------------------------------------------------- notifications

    def rules(self, sources: dict[str, Any]) -> list[dict[str, Any]]:
        """Return the notification rules (if push is on for "Saugroboter")."""
        cfg = sources.get("hh.robot") or {}
        if not cfg.get("push") or not (vacuum := self.entity_id):
            return []
        base = {
            "src": "hh.robot",
            "title": "Saugroboter",
            "urgent": bool(cfg.get("urgent")),
            "url": cfg.get("url") or "/",
        }
        rules: list[dict[str, Any]] = [
            {
                **base,
                "id": f"vacuum|{self.id}|error",
                "type": "state",
                "entity": vacuum,
                "to": ["error"],
                "message": f"{self.name}: Fehler",
            }
        ]
        no_error = list(NO_ERROR)
        for key, pattern, text in (
            ("vacerr", r"(vacuum_error|roboterfehler|sauger.?fehler|vacuum error)", "Störung"),
            (
                "dockerr",
                r"(dock_error|stationsfehler|dock.?fehler|dock error)",
                "Station braucht dich",
            ),
        ):
            if entity := self._find("sensor", pattern):
                rules.append(
                    {
                        **base,
                        "id": f"vacuum|{self.id}|{key}",
                        "type": "template",
                        "template": f"{{{{ states('{entity}') | lower not in {no_error} }}}}",
                        "for": 60,
                        "message": f"{self.name}: {text}",
                    }
                )
        for entry in self.entities():
            if entry.domain != "binary_sensor":
                continue
            text = self._text(entry)
            state = self.hass.states.get(entry.entity_id)
            device_class = state.attributes.get("device_class") if state else None
            need = next((t for p, t in STATION if re.search(p, text)), None)
            if need is None and device_class != "problem":
                continue
            if need is None:
                name = (
                    state.attributes.get("friendly_name", entry.entity_id)
                    if state
                    else entry.entity_id
                )
                need = re.sub(rf"^{re.escape(self.name)}\s*", "", str(name), flags=re.IGNORECASE)
            rules.append(
                {
                    **base,
                    "id": f"vacuum|{self.id}|station|{entry.entity_id}",
                    "type": "state",
                    "entity": entry.entity_id,
                    "to": ["on"],
                    "for": 60,
                    "message": f"{self.name}: {need}",
                }
            )
        for entry in self.entities():
            if entry.domain != "sensor":
                continue
            text = self._text(entry)
            for i, (key, bad, need) in enumerate(STATION_SENSORS):
                if not re.search(key, text) or (i == 0 and _DIRTY.search(text)):
                    continue
                rules.append(
                    {
                        **base,
                        "id": f"vacuum|{self.id}|tank|{entry.entity_id}",
                        "type": "template",
                        "template": (
                            f"{{{{ states('{entry.entity_id}') | lower is search('{bad}') }}}}"
                        ),
                        "for": 60,
                        "message": f"{self.name}: {need}",
                    }
                )
                break
        for entry in self.entities():
            if entry.domain != "sensor":
                continue
            text = self._text(entry)
            if not re.search(r"(time_left|verbleibend|restzeit|remaining|lebensdauer)", text):
                continue
            if re.search(r"(dry|trocknung)", text):
                continue
            kind = next((k for k in CONSUMABLES if re.search(k[0], text)), None)
            state = self.hass.states.get(entry.entity_id)
            unit = state.attributes.get("unit_of_measurement", "h") if state else "h"
            if kind is None or unit not in _HOURS:
                continue
            _pattern, name, life, verb = kind
            limit = life * 0.1 / _HOURS[unit]
            rules.append(
                {
                    **base,
                    "id": f"vacuum|{self.id}|wear|{entry.entity_id}",
                    "type": "template",
                    "template": (
                        f"{{{{ states('{entry.entity_id}') | float(999999) <= {limit:.4g} }}}}"
                    ),
                    "message": f"{self.name}: {name} {verb}",
                }
            )
        return rules


class VacuumManager:
    """All configured robot vacuums of the config entry."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self.trackers: dict[str, VacuumTracker] = {}
        self._unsub: list[CALLBACK_TYPE] = []
        self.on_unusual: Callable[[Any, str, dict[str, Any]], None] | None = None

    @callback
    def async_setup(self, subentries: list[ConfigSubentry]) -> None:
        """Start tracking the vacuums."""
        for subentry in subentries:
            if subentry.subentry_type == SUBENTRY_VACUUM:
                tracker = VacuumTracker(self.hass, self.store, subentry)
                tracker.on_unusual = self.on_unusual
                self.trackers[subentry.subentry_id] = tracker
        if not self.trackers:
            return
        registry = er.async_get(self.hass)

        def tracker_of(entity_id: str) -> VacuumTracker | None:
            entry = registry.async_get(entity_id)
            if entry is None or entry.device_id is None:
                return None
            return next(
                (t for t in self.trackers.values() if entry.device_id in t.device_ids()), None
            )

        @callback
        def is_vacuum_entity(data: EventStateChangedData) -> bool:
            entity_id = data["entity_id"]
            return (
                entity_id.startswith(("vacuum.", "sensor.")) and tracker_of(entity_id) is not None
            )

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            if tracker := tracker_of(event.data["entity_id"]):
                tracker.evaluate()

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=is_vacuum_entity)
        )
        for tracker in self.trackers.values():
            tracker.evaluate()

    @callback
    def async_stop(self) -> None:
        """Stop tracking."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def rules(self) -> list[dict[str, Any]]:
        """Return the notification rules of all vacuums."""
        sources = self.store.data["notify"].get("sources") or {}
        return [rule for t in self.trackers.values() for rule in t.rules(sources)]

    def info(self) -> list[dict[str, Any]]:
        """Return the vacuums for the dashboard."""
        return [t.info() for t in self.trackers.values()]
