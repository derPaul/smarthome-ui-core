"""Security: open windows and doors (rain, everybody left, good night) and last night.

Checks (each can be switched off on the device "Sicherheit"), sent as notification with the source
"Fenster & Türen" (``sec.openings``):

* **rain** – something is open and it rains or rain is coming within ``RAIN_AHEAD_HOURS``
  (weather state and hourly forecast): "Regen ab 15 Uhr – noch offen: Bad, Küche",
* **everybody left** – the last person left home and something is open or unlocked,
* **good night** – at a set time, while somebody is home: open windows, unlocked doors, alarm off.

**Last night** (``sec.night``): between the start and end of the night (22:00–06:00 by default)
Core counts person detections (cameras), animals, vehicles (if wanted), motion in the house and
opened doors as they happen. At the end of the night it sends a summary if a person was detected
or a door was opened.

Which sensors count comes from the dashboard (``security/settings``) or is found like the
dashboard does it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import re
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_change, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SIGNAL_SECURITY
from .store import CoreStore, now_ms

OPENING_CLASSES = ("window", "door", "garage_door", "opening")
DOOR_CLASSES = ("door", "garage_door")
MOTION_CLASSES = ("motion", "occupancy", "presence")
APPLIANCE_PLATFORMS = (
    "home_connect",
    "miele",
    "electrolux",
    "electrolux_status",
    "lg_thinq",
    "smartthings",
    "whirlpool",
    "candy",
    "haier",
    "plant",
)
_APPLIANCE_NAME = re.compile(
    r"(kühl|kuehl|fridge|gefrier|freezer|spül|spuel|dishwasher|ofen|oven|wasch|washer|trockner|"
    r"dryer|briefkasten|mailbox|microwave|mikrowelle)",
    re.IGNORECASE,
)
_OUTDOOR = re.compile(
    r"(aussen|außen|outdoor|outside|garten|einfahrt|terrasse|balkon|hof|carport)", re.IGNORECASE
)
NIGHT_CATS: dict[str, re.Pattern[str] | None] = {
    "persons": re.compile(r" (person|personen|people|human|mensch)( |$)"),
    "animals": re.compile(r" (animal|animals|tier|tiere|pet|dog|cat|hund|katze|bird|vogel)( |$)"),
    "vehicles": re.compile(r" (vehicle|fahrzeug|car|auto)( |$)"),
    "motion": None,
    "doors": None,
}
RAINY = ("rainy", "pouring", "lightning-rainy", "snowy-rainy", "hail")
RAIN_AHEAD_HOURS = 2
EVENTS_MAX = 200
DEFAULTS: dict[str, Any] = {
    "openings": None,  # entity list from the dashboard, None = found automatically
    "night": {
        "start": "22:00",
        "end": "06:00",
        "persons": None,
        "animals": None,
        "vehicles": None,
        "motion": None,
    },
    "checks": {"rain": True, "leave": True, "good_night": False, "good_night_at": "22:30"},
}


def _hm(value: Any, default: str) -> tuple[int, int]:
    match = re.match(r"^(\d{1,2}):?(\d{2})?", str(value or default))
    if not match:
        return _hm(default, default)
    return int(match.group(1)) % 24, int(match.group(2) or 0) % 60


def _text(state: State) -> str:
    name = state.attributes.get("friendly_name", "")
    return " " + re.sub(r"[^a-z0-9äöüß]+", " ", f"{state.entity_id} {name}".lower()) + " "


def names(items: list[str], limit: int = 4) -> str:
    """Return "Bad, Küche und 2 weitere"."""
    if len(items) <= limit:
        return ", ".join(items)
    return f"{', '.join(items[:limit])} und {len(items) - limit} weitere"


class Security:
    """Open windows and doors, checks, last night."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        watchers: Any,
        notify: Callable[[str, str, str, str], None],
        skip_devices: Callable[[], set[str]] = set,
    ) -> None:
        """Initialize (notify(src, title, message, tag); skip_devices: appliance devices)."""
        self.hass = hass
        self.store = store
        self.watchers = watchers
        self.notify = notify
        self.skip_devices = skip_devices
        self._unsub: list[CALLBACK_TYPE] = []
        self._home: int | None = None
        self._rain_key: str | None = None
        self._listeners: list[CALLBACK_TYPE] = []
        self._timers: list[CALLBACK_TYPE] = []

    # ---------------------------------------------------------------- settings

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored settings and the night log."""
        return self.store.data["security"]

    def checks(self) -> dict[str, Any]:
        """Return which checks are on (and the good night time)."""
        return {**DEFAULTS["checks"], **(self.data.get("checks") or {})}

    def night_settings(self) -> dict[str, Any]:
        """Return start, end and own sensor lists of the night."""
        return {**DEFAULTS["night"], **(self.data.get("night") or {})}

    @callback
    def set_check(self, key: str, value: Any) -> None:
        """Switch a check on/off or set the good night time."""
        self.data["checks"] = {**self.checks(), key: value}
        self.store.save()
        async_dispatcher_send(self.hass, SIGNAL_SECURITY)
        if key == "good_night_at":
            self._schedule()

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Take openings and night settings from the dashboard."""
        if "openings" in settings:
            self.data["openings"] = settings["openings"]
        if "night" in settings and settings["night"] is not None:
            self.data["night"] = {**self.night_settings(), **settings["night"]}
        self.store.save()
        self._listen()
        self._schedule()
        async_dispatcher_send(self.hass, SIGNAL_SECURITY)

    # ---------------------------------------------------------------- sensors

    def _usable(self, state: State) -> bool:
        entry = er.async_get(self.hass).async_get(state.entity_id)
        if entry is None:
            return True
        if entry.hidden_by or entry.disabled_by or entry.entity_category:
            return False
        return entry.platform not in APPLIANCE_PLATFORMS and entry.device_id not in (
            self.skip_devices() or set()
        )

    def openings(self) -> list[str]:
        """Return the window and door sensors (from the dashboard or found)."""
        own = self.data.get("openings")
        if isinstance(own, list):
            return [e for e in own if self.hass.states.get(e) is not None]
        return [
            s.entity_id
            for s in self.hass.states.async_all("binary_sensor")
            if s.attributes.get("device_class") in OPENING_CLASSES
            and not _APPLIANCE_NAME.search(_text(s))
            and self._usable(s)
        ]

    def _name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return str(state.attributes.get("friendly_name") or entity_id) if state else entity_id

    def open_now(self) -> list[str]:
        """Return the open windows and doors."""
        return [e for e in self.openings() if (s := self.hass.states.get(e)) and s.state == "on"]

    def unlocked(self) -> list[str]:
        """Return the unlocked locks."""
        return [
            s.entity_id
            for s in self.hass.states.async_all("lock")
            if s.state in ("unlocked", "open", "opening") and self._usable(s)
        ]

    def alarm_off(self) -> list[str]:
        """Return the alarm panels that are disarmed."""
        return [
            s.entity_id
            for s in self.hass.states.async_all("alarm_control_panel")
            if s.state == "disarmed"
        ]

    def home_count(self) -> int:
        """Return how many persons are home."""
        return sum(1 for s in self.hass.states.async_all("person") if s.state == "home")

    def night_sensors(self) -> dict[str, list[str]]:
        """Return the sensors per night category (from the dashboard or found)."""
        cfg = self.night_settings()
        bins = [
            s
            for s in self.hass.states.async_all("binary_sensor")
            if self._usable(s) or er.async_get(self.hass).async_get(s.entity_id) is None
        ]
        found: dict[str, list[str]] = {}
        for key, pattern in NIGHT_CATS.items():
            if pattern is not None:
                found[key] = [s.entity_id for s in bins if pattern.search(_text(s))]
        taken = {e for ids in found.values() for e in ids}
        registry = er.async_get(self.hass)
        cam_devices = {
            entry.device_id
            for s in self.hass.states.async_all("camera")
            if (entry := registry.async_get(s.entity_id)) and entry.device_id
        }

        def device(entity_id: str) -> str | None:
            entry = registry.async_get(entity_id)
            return entry.device_id if entry else None

        found["motion"] = [
            s.entity_id
            for s in bins
            if s.attributes.get("device_class") in MOTION_CLASSES
            and s.entity_id not in taken
            and device(s.entity_id) not in cam_devices
            and not _OUTDOOR.search(_text(s))
        ]
        found["doors"] = [
            e
            for e in self.openings()
            if (s := self.hass.states.get(e)) and s.attributes.get("device_class") in DOOR_CLASSES
        ]
        out = {}
        for key in NIGHT_CATS:
            own = cfg.get(key)
            out[key] = list(own) if isinstance(own, list) else found.get(key, [])
        if cfg.get("vehicles") is None:
            out["vehicles"] = []  # vehicles only when chosen
        return out

    # ---------------------------------------------------------------- setup

    @callback
    def async_setup(self) -> None:
        """Start the checks and the night log."""
        self._home = self.home_count()
        self._listen()
        self._schedule()
        self._unsub.append(
            async_track_time_interval(self.hass, self._check_rain, timedelta(minutes=5))
        )
        self._start_night_if_needed()

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in [*self._unsub, *self._listeners, *self._timers]:
            unsub()
        self._unsub.clear()
        self._listeners = []
        self._timers = []

    @callback
    def _listen(self) -> None:
        for unsub in self._listeners:
            unsub()
        openings = set(self.openings())
        night = self.night_sensors()
        cats = {e: k for k, ids in night.items() for e in ids}

        @callback
        def relevant(data: EventStateChangedData) -> bool:
            eid = data["entity_id"]
            return eid in openings or eid in cats or eid.startswith("person.")

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            eid = event.data["entity_id"]
            old, new = event.data["old_state"], event.data["new_state"]
            if eid.startswith("person."):
                self._persons_changed()
                return
            if eid in cats and new is not None and new.state == "on":
                if old is None or old.state != "on":
                    self._night_event(eid, cats[eid])
            if eid in openings:
                async_dispatcher_send(self.hass, SIGNAL_SECURITY)
                if new is not None and new.state == "on":
                    self._check_rain()

        self._listeners = [
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=relevant)
        ]

    @callback
    def _schedule(self) -> None:
        for unsub in self._timers:
            unsub()
        night = self.night_settings()
        sh, sm = _hm(night["start"], "22:00")
        eh, em = _hm(night["end"], "06:00")
        gh, gm = _hm(self.checks()["good_night_at"], "22:30")
        self._timers = [
            async_track_time_change(self.hass, self._night_start, hour=sh, minute=sm, second=0),
            async_track_time_change(self.hass, self._night_end, hour=eh, minute=em, second=0),
            async_track_time_change(self.hass, self._good_night, hour=gh, minute=gm, second=0),
        ]

    # ---------------------------------------------------------------- checks

    def _open_text(self) -> str:
        parts = []
        if opened := self.open_now():
            parts.append(f"noch offen: {names([self._name(e) for e in opened])}")
        if unlocked := self.unlocked():
            parts.append(f"nicht abgeschlossen: {names([self._name(e) for e in unlocked])}")
        return " · ".join(parts)

    def rain_coming(self) -> datetime | None:
        """Return when rain starts (now if it rains), None if no rain within the next hours."""
        now = dt_util.utcnow()
        if any(s.state in RAINY for s in self.hass.states.async_all("weather")):
            return now
        end = now + timedelta(hours=RAIN_AHEAD_HOURS)
        for item in getattr(self.watchers, "hourly", None) or []:
            when = dt_util.parse_datetime(str(item.get("datetime", "")))
            if when is None or when > end or when < now - timedelta(hours=1):
                continue
            prob = item.get("precipitation_probability")
            amount = float(item.get("precipitation") or 0)
            rainy = item.get("condition") in RAINY or amount >= 0.5
            if rainy and (prob is None or float(prob) >= 60):
                return max(when, now)
        return None

    @callback
    def _check_rain(self, _now: Any = None) -> None:
        if not self.checks()["rain"]:
            return
        when = self.rain_coming()
        if when is None:
            self._rain_key = None
            return
        opened = self.open_now()
        if not opened:
            return
        local = dt_util.as_local(when)
        key = local.strftime("%Y-%m-%d %H")
        if self._rain_key is not None:
            return  # one warning per rain
        self._rain_key = key
        soon = when - dt_util.utcnow() <= timedelta(minutes=10)
        head = "Es regnet" if soon else f"Regen ab {local.hour} Uhr"
        self.notify(
            "sec.openings",
            "Fenster & Türen",
            f"{head} – noch offen: {names([self._name(e) for e in opened])}",
            "security|rain",
        )

    @callback
    def _persons_changed(self) -> None:
        count = self.home_count()
        before, self._home = self._home, count
        async_dispatcher_send(self.hass, SIGNAL_SECURITY)
        if not self.checks()["leave"] or not before or count:
            return
        if text := self._open_text():
            self.notify("sec.openings", "Alle weg", text[0].upper() + text[1:], "security|leave")

    @callback
    def _good_night(self, _now: Any = None) -> None:
        if not self.checks()["good_night"] or not self.home_count():
            return
        parts = [self._open_text()] if self._open_text() else []
        if self.alarm_off():
            parts.append("Alarm nicht scharf")
        if parts:
            text = " · ".join(parts)
            self.notify(
                "sec.openings", "Gute Nacht", text[0].upper() + text[1:], "security|goodnight"
            )

    # ---------------------------------------------------------------- last night

    def night_window(self, now: datetime | None = None) -> tuple[datetime, datetime, bool]:
        """Return start and end of the current (or last) night and whether it is running."""
        now = dt_util.as_local(now or dt_util.utcnow())
        night = self.night_settings()
        sh, sm = _hm(night["start"], "22:00")
        eh, em = _hm(night["end"], "06:00")
        duration = timedelta(minutes=((eh * 60 + em - sh * 60 - sm) % 1440) or 1440)
        end_today = now.replace(hour=eh, minute=em, second=0, microsecond=0)
        for end in (end_today + timedelta(days=1), end_today, end_today - timedelta(days=1)):
            if end - duration <= now < end:
                return end - duration, end, True
        end = end_today if end_today <= now else end_today - timedelta(days=1)
        return end - duration, end, False

    def _log(self) -> dict[str, Any]:
        return self.data.setdefault("night_log", {"start": 0, "events": [], "sent": False})

    @callback
    def _start_night_if_needed(self) -> None:
        start, _end, running = self.night_window()
        log = self._log()
        if running and int(log.get("start") or 0) < int(start.timestamp() * 1000):
            self._night_start()

    @callback
    def _night_start(self, _now: Any = None) -> None:
        start, _end, running = self.night_window()
        # only at the start of a running night, and only once per night
        if not running or int(self._log().get("start") or 0) >= int(start.timestamp() * 1000):
            return
        self.data["night_log"] = {
            "start": int(start.timestamp() * 1000),
            "events": [],
            "sent": False,
        }
        self.store.save()
        async_dispatcher_send(self.hass, SIGNAL_SECURITY)

    @callback
    def _night_event(self, entity_id: str, cat: str) -> None:
        _start, _end, running = self.night_window()
        if not running:
            return
        self._start_night_if_needed()
        events = self._log()["events"]
        events.append({"t": now_ms(), "id": entity_id, "cat": cat})
        del events[:-EVENTS_MAX]
        self.store.save()
        async_dispatcher_send(self.hass, SIGNAL_SECURITY)

    def night_summary(self) -> dict[str, Any]:
        """Return start, end, counts per category and the events of the night."""
        start, end, running = self.night_window()
        log = self._log()
        current = int(log.get("start") or 0) >= int((start.timestamp() - 60) * 1000)
        events = (log.get("events") or []) if current else []
        counts = {key: 0 for key in NIGHT_CATS}
        for event in events:
            counts[event["cat"]] = counts.get(event["cat"], 0) + 1
        return {
            "start": int(start.timestamp() * 1000),
            "end": int(end.timestamp() * 1000),
            "running": running,
            "counts": counts,
            "events": [{**e, "name": self._name(e["id"])} for e in events[-50:]],
        }

    def night_message(self) -> str | None:
        """Return the morning summary (None if nothing worth telling happened)."""
        summary = self.night_summary()
        counts, events = summary["counts"], summary["events"]
        if not counts["persons"] and not counts["doors"]:
            return None

        def first(cat: str) -> str:
            hit = next((e for e in events if e["cat"] == cat), None)
            if hit is None:
                return ""
            at = dt_util.as_local(dt_util.utc_from_timestamp(hit["t"] / 1000)).strftime("%H:%M")
            return f" ({hit['name']} {at})"

        parts = []
        if n := counts["persons"]:
            parts.append(f"{n} {'Person' if n == 1 else 'Personen'} erkannt{first('persons')}")
        if n := counts["doors"]:
            parts.append(f"{n}× Tür geöffnet{first('doors')}")
        if n := counts["animals"]:
            parts.append(f"{n}× Tier")
        if n := counts["vehicles"]:
            parts.append(f"{n}× Fahrzeug")
        if n := counts["motion"]:
            parts.append(f"{n}× Bewegung im Haus")
        return " · ".join(parts)

    @callback
    def _night_end(self, _now: Any = None) -> None:
        log = self._log()
        if log.get("sent"):
            return
        log["sent"] = True
        self.store.save()
        async_dispatcher_send(self.hass, SIGNAL_SECURITY)
        if message := self.night_message():
            self.notify("sec.night", "Letzte Nacht", message, "security|night")

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        return {
            "open": self.open_now(),
            "unlocked": self.unlocked(),
            "home": self.home_count(),
            "checks": self.checks(),
            "night": self.night_summary(),
        }
