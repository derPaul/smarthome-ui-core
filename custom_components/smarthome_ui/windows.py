"""Windows as devices: cover, contact, orientation and cleaning per window, plus their automations.

Every shutter or blind (``cover`` with device class shutter/blind/shade or none; not garage doors or
awnings) becomes a window on its own, including ones added later (a new Eve Shutter Switch joins
by itself). A window can also be added without a cover (contact only). Per window:

* ``cover``, ``contact`` (optional, guessed when its area has exactly one window contact), ``door``
  (a terrace door: lockout protection), ``orientation`` (n, ne, e, se, s, sw, w, nw – the facade the
  window looks to; needed for the heat protection), ``auto`` (takes part in the automations),
  ``cleaned`` (ms, last cleaned).

Automations (``settings``; morning, evening and heat protection start switched off, so nothing moves
before they are chosen in the dashboard – existing automations of the covers keep working):

* **morning** – open at ``morning`` on weekdays, ``morning_weekend`` on weekends,
* **evening** – close ``evening_offset`` minutes after sunset,
* **heat protection** – when the sun shines on the facade (azimuth within 70° of the facade,
  elevation at least 12°) and it is at least ``heat_temp`` °C outside: down to ``heat_position`` %;
  back to where it was when the sun has gone (unless someone moved it meanwhile),
* **lockout protection** – an open door is never closed; the evening close follows once it is shut,
* **winter mode** (``winter``: auto below 8 °C outside, on, off) – close right at sunset, open not
  before 15 minutes after sunrise, no heat protection.
* **brightness** (``trigger`` "dark" instead of "time"): a "dark" sensor (e.g. a camera's
  ``binary_sensor.…_ist_dunkel``) decides instead of the clock and the sun – closed once it has been
  dark for ``dark_delay`` minutes, opened once it has been light as long, but not before the morning
  time. Changes from or to unavailable (a restart) do not count.

Morning and evening move the covers one after another, ``stagger`` seconds apart (give or take a
half, in the order of the rooms) – as if someone walked through the house.

Moved by hand: the heat protection leaves that window alone for 3 hours. Covers still chosen in the
presence simulation are left to it while it runs; all others keep their rhythm, also while away.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import random
import re
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Context, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import (
    async_call_later,
    async_track_point_in_utc_time,
    async_track_state_change_event,
    async_track_sunset,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util, slugify

from .chores import start_of_day
from .const import DOMAIN, SIGNAL_DATA, SIGNAL_TASKS, SIGNAL_WINDOWS
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

ORIENTATIONS: dict[str, int] = {
    "n": 0,
    "ne": 45,
    "e": 90,
    "se": 135,
    "s": 180,
    "sw": 225,
    "w": 270,
    "nw": 315,
}
DEFAULTS: dict[str, Any] = {
    "morning": "07:00",
    "morning_weekend": "08:30",
    "morning_on": False,
    "evening_on": False,
    "evening_offset": 15,
    "heat_on": False,
    "heat_temp": 24.0,
    "heat_position": 25,
    "lockout": True,
    "winter": "auto",
    "outdoor": None,
    "clean_days": 90,
    "trigger": "time",
    "dark_sensor": None,
    "dark_delay": 10,
    "stagger": 45,
}
SUN_SPREAD = 70
SUN_MIN_ELEVATION = 12
WINTER_BELOW = 8.0
MANUAL_HOLD = timedelta(hours=3)
HEAT_INTERVAL = timedelta(minutes=10)
COVER_CLASSES = ("shutter", "blind", "shade", "curtain", "window", None)
_NOT_WINDOW = re.compile(r"garage|\btor\b|_tor\b|gate|markise|awning|sonnensegel")
_CONTACT_CLASSES = ("window", "door", "opening", "garage_door")
_DARK = re.compile(r"ist_dunkel|is_dark|_dark$|dunkel")
_GONE = ("unknown", "unavailable")


def _hm(value: Any, default: str) -> tuple[int, int]:
    try:
        hour, minute = str(value or default).split(":")[:2]
        return int(hour) % 24, int(minute) % 60
    except ValueError:
        hour, minute = default.split(":")
        return int(hour), int(minute)


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def facade_lit(orientation: str | None, azimuth: float | None, elevation: float | None) -> bool:
    """Return True if the sun shines on a facade looking to ``orientation``."""
    if orientation not in ORIENTATIONS or azimuth is None or elevation is None:
        return False
    diff = abs((azimuth - ORIENTATIONS[orientation] + 180) % 360 - 180)
    return elevation >= SUN_MIN_ELEVATION and diff <= SUN_SPREAD


class Windows:
    """The windows and their automations."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self._unsub: list[CALLBACK_TYPE] = []
        self._sched: list[CALLBACK_TYPE] = []
        self._ctx: dict[str, float] = {}
        self._dark: list[CALLBACK_TYPE] = []
        self._seq: asyncio.Task[None] | None = None
        # set by the integration: is morning/evening someone else's job right now?
        self.others_busy: Callable[[str], bool] = lambda cover: False

    # ---------------------------------------------------------------- data

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored windows and settings."""
        return self.store.data.setdefault("windows", {})

    @property
    def items(self) -> dict[str, dict[str, Any]]:
        """Return the windows by id."""
        return self.data.setdefault("items", {})

    def settings(self) -> dict[str, Any]:
        """Return the settings with defaults."""
        return {**DEFAULTS, **(self.data.get("settings") or {})}

    def _save(self) -> None:
        self.store.save(SIGNAL_DATA, SIGNAL_TASKS)
        async_dispatcher_send(self.hass, SIGNAL_WINDOWS)

    def set_settings(self, settings: dict[str, Any]) -> None:
        """Change the settings."""
        own = dict(self.data.get("settings") or {})
        own.update({k: v for k, v in settings.items() if k in DEFAULTS})
        if own.get("winter") not in ("auto", "on", "off"):
            own.pop("winter", None)
        if own.get("trigger") not in ("time", "dark"):
            own.pop("trigger", None)
        self.data["settings"] = own
        self._schedule()
        self._save()

    def set_window(self, wid: str | None, values: dict[str, Any]) -> dict[str, Any]:
        """Add or change a window (``wid`` None: a new one, e.g. without a cover)."""
        allowed = ("name", "area_id", "cover", "contact", "lock", "door", "orientation", "auto")
        if wid is None or wid not in self.items:
            base = slugify(values.get("name") or values.get("cover") or "fenster")
            base = base.removeprefix("cover_").removeprefix("fenster_") or "fenster"
            wid = base
            n = 2
            while wid in self.items:
                wid = f"{base}_{n}"
                n += 1
            self.items[wid] = {"name": values.get("name") or "Fenster", "auto": True}
        item = self.items[wid]
        for key in allowed:
            if key in values:
                item[key] = values[key]
        if item.get("orientation") not in ORIENTATIONS:
            item["orientation"] = None
        self._save()
        return item

    def remove(self, wid: str) -> bool:
        """Remove a window (a cover removed this way does not come back by itself)."""
        item = self.items.pop(wid, None)
        if item is None:
            return False
        if item.get("cover"):
            ignored = self.data.setdefault("ignored", [])
            if item["cover"] not in ignored:
                ignored.append(item["cover"])
        self._save()
        return True

    def cleaned(self, wids: list[str], when: int | None = None) -> None:
        """Mark windows as cleaned (now)."""
        for wid in wids:
            if wid in self.items:
                self.items[wid]["cleaned"] = when or now_ms()
        self._save()

    @staticmethod
    def room_key(area_id: str | None) -> str:
        """Return the task key of cleaning the windows of a room."""
        return f"window_clean|{area_id or 'none'}"

    def last_cleaned(self, item: dict[str, Any]) -> int | None:
        """Return when the window was cleaned (on its own or with its room's task)."""
        room = int((self.store.done.get(self.room_key(item.get("area_id"))) or {}).get("t") or 0)
        return max(int(item.get("cleaned") or 0), room) or None

    def clean_due(self, item: dict[str, Any]) -> bool:
        """Return True when the window is due for cleaning (never cleaned counts as due)."""
        days = _num(self.settings()["clean_days"]) or 0
        if days <= 0:
            return False
        last = self.last_cleaned(item)
        return last is None or now_ms() - last >= days * 86_400_000

    def tasks(self) -> list[dict[str, Any]]:
        """Return "Fenster putzen" per room (due with its longest-unclean window)."""
        days = _num(self.settings()["clean_days"]) or 0
        if days <= 0 or not self.items:
            return []
        rooms: dict[str | None, list[dict[str, Any]]] = {}
        for item in self.items.values():
            rooms.setdefault(item.get("area_id"), []).append(item)
        areas = ar.async_get(self.hass)
        out = []
        for area_id, items in rooms.items():
            lasts = [self.last_cleaned(i) for i in items]
            last = None if None in lasts else min(lasts)
            area = areas.async_get_area(area_id) if area_id else None
            out.append(
                {
                    "key": self.room_key(area_id),
                    "name": "Fenster putzen" + (f" ({len(items)})" if len(items) > 1 else ""),
                    "src": "hh.tasks",
                    "area": area.name if area else None,
                    "icon": "mdi:window-closed-variant",
                    "interval": days,
                    "last": last,
                    "due": start_of_day(
                        (last or now_ms() - int(days * 86_400_000)) + int(days * 86_400_000)
                    ),
                }
            )
        return out

    # ---------------------------------------------------------------- discovery

    def _eligible(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        if not entity_id.startswith("cover.") or _NOT_WINDOW.search(entity_id):
            return False
        cls = state.attributes.get("device_class") if state is not None else None
        name = str(state.attributes.get("friendly_name", "")) if state is not None else ""
        return cls in COVER_CLASSES and not _NOT_WINDOW.search(name.lower())

    def _area_of(self, entity_id: str) -> str | None:
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry is None:
            return None
        if entry.area_id:
            return entry.area_id
        device = dr.async_get(self.hass).async_get(entry.device_id) if entry.device_id else None
        return device.area_id if device else None

    @callback
    def discover(self) -> list[str]:
        """Make a window of every shutter that is not one yet (new Eve switches join by itself)."""
        linked = {w.get("cover") for w in self.items.values()}
        ignored = set(self.data.get("ignored") or [])
        areas = ar.async_get(self.hass)
        added = []
        covers = [
            s.entity_id
            for s in self.hass.states.async_all("cover")
            if self._eligible(s.entity_id) and s.entity_id not in linked | ignored
        ]
        for cover in covers:
            area_id = self._area_of(cover)
            area = areas.async_get_area(area_id) if area_id else None
            in_area = [
                c
                for c in [*covers, *linked]
                if c and area_id and self._area_of(c) == area_id and self._eligible(c)
            ]
            state = self.hass.states.get(cover)
            name = str(state.attributes.get("friendly_name") or cover) if state else cover
            if area is not None and len(in_area) == 1:
                name = f"Fenster {area.name}"
            item = self.set_window(
                None, {"name": name, "cover": cover, "area_id": area_id, "auto": True}
            )
            if contact := self._guess_contact(area_id):
                item["contact"] = contact
            added.append(cover)
        if added:
            self._save()
        return added

    def _guess_contact(self, area_id: str | None) -> str | None:
        """Return the window contact of the area, if there is exactly one and no window uses it."""
        if not area_id:
            return None
        used = {w.get("contact") for w in self.items.values()}
        found = [
            s.entity_id
            for s in self.hass.states.async_all("binary_sensor")
            if s.attributes.get("device_class") in ("window", "opening")
            and self._area_of(s.entity_id) == area_id
        ]
        return found[0] if len(found) == 1 and found[0] not in used else None

    # ---------------------------------------------------------------- state

    def is_open(self, item: dict[str, Any]) -> bool | None:
        """Return True if the window (or door) is open, None without a contact."""
        contact = item.get("contact")
        state = self.hass.states.get(contact) if contact else None
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        return state.state == "on"

    def position(self, item: dict[str, Any]) -> float | None:
        """Return the cover position (0 closed, 100 open)."""
        state = self.hass.states.get(item.get("cover") or "")
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        pos = _num(state.attributes.get("current_position"))
        if pos is None:
            pos = 0.0 if state.state == "closed" else 100.0
        return pos

    def outdoor(self) -> float | None:
        """Return the outdoor temperature (own sensor, else the first weather entity)."""
        if own := self.settings()["outdoor"]:
            state = self.hass.states.get(own)
            return _num(state.state) if state is not None else None
        for state in self.hass.states.async_all("weather"):
            if (value := _num(state.attributes.get("temperature"))) is not None:
                return value
        return None

    def winter(self) -> bool:
        """Return True while the winter mode applies."""
        mode = self.settings()["winter"]
        if mode in ("on", "off"):
            return mode == "on"
        temp = self.outdoor()
        return temp is not None and temp < WINTER_BELOW

    def sun(self) -> tuple[float | None, float | None]:
        """Return the sun's azimuth and elevation."""
        state = self.hass.states.get("sun.sun")
        if state is None:
            return None, None
        return _num(state.attributes.get("azimuth")), _num(state.attributes.get("elevation"))

    def _active(self) -> list[tuple[str, dict[str, Any]]]:
        return [
            (wid, w)
            for wid, w in self.items.items()
            if w.get("auto", True) and w.get("cover") and self.hass.states.get(w["cover"])
        ]

    # ---------------------------------------------------------------- moving

    async def _move(self, item: dict[str, Any], position: float | None, why: str) -> bool:
        """Move a cover (None: close); an open door is never closed."""
        cover = item["cover"]
        closing = position is None or position < (self.position(item) or 0)
        if closing and item.get("door") and self.settings()["lockout"] and self.is_open(item):
            item["pending"] = why
            _LOGGER.debug("Lockout: %s stays open (%s)", cover, why)
            return False
        context = Context()
        self._ctx[context.id] = dt_util.utcnow().timestamp()
        if position is None:
            service, data = "close_cover", {"entity_id": cover}
        elif position >= 100:
            service, data = "open_cover", {"entity_id": cover}
        else:
            service, data = "set_cover_position", {"entity_id": cover, "position": round(position)}
        try:
            await self.hass.services.async_call(
                "cover", service, data, blocking=True, context=context
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("%s %s failed: %s", service, cover, err)
            return False
        item["last"] = {"t": now_ms(), "why": why}
        item.pop("pending", None)
        return True

    async def async_morning(self) -> None:  # noqa: RUF029
        """Open the covers in the morning."""
        if self.winter():
            sunrise = self._next("sunrise")
            if sunrise is not None and sunrise - dt_util.utcnow() < timedelta(hours=12):
                # the sun is not up yet: open 15 minutes after sunrise instead (once)
                self._sched.append(
                    async_track_point_in_utc_time(
                        self.hass,
                        self._run(lambda: self.async_morning_now(stagger=True)),
                        sunrise + timedelta(minutes=15),
                    )
                )
                return
        self._start(100, "morning")

    def _walk(self) -> list[dict[str, Any]]:
        """Return the active windows in the order of a walk through the rooms."""
        areas = ar.async_get(self.hass)

        def room(w: dict[str, Any]) -> str:
            area = areas.async_get_area(w["area_id"]) if w.get("area_id") else None
            return area.name if area else "~"

        return sorted(
            (w for _wid, w in self._active()),
            key=lambda w: (room(w), str(w.get("name") or "")),
        )

    async def _sequence(self, position: float | None, why: str, stagger: bool) -> None:
        """Move every window (not those someone else runs), one after another when staggering."""
        gap = float(_num(self.settings()["stagger"]) or 0) if stagger else 0.0
        first = True
        for w in self._walk():
            if self.others_busy(w["cover"]):
                continue
            if not first and gap > 0:
                await asyncio.sleep(gap * random.uniform(0.5, 1.5))
            first = False
            w.pop("heat", None)
            await self._move(w, position, why)
        self.data["last_run"] = {"why": why, "t": now_ms()}
        self._save()

    def _start(self, position: float | None, why: str) -> None:
        """Start a staggered run (a newer one replaces one still walking)."""
        if self._seq is not None and not self._seq.done():
            self._seq.cancel()
        self._seq = self.hass.async_create_background_task(
            self._sequence(position, why, stagger=True), f"{DOMAIN} windows {why}"
        )

    async def async_morning_now(self, stagger: bool = False) -> None:
        """Open every window's cover (not those someone else runs right now)."""
        await self._sequence(100, "morning", stagger)

    async def async_evening(self, stagger: bool = False) -> None:
        """Close the covers in the evening (open doors follow once shut)."""
        await self._sequence(None, "evening", stagger)

    async def async_heat(self, _now: Any = None) -> None:
        """Heat protection: down while the sun is on the facade, back afterwards."""
        s = self.settings()
        azimuth, elevation = self.sun()
        temp = self.outdoor()
        hot = (
            s["heat_on"]
            and not self.winter()
            and temp is not None
            and temp >= float(s["heat_temp"])
        )
        target = float(s["heat_position"])
        now = dt_util.utcnow().timestamp()
        changed = False
        for _wid, w in self._active():
            lit = facade_lit(w.get("orientation"), azimuth, elevation)
            manual = w.get("manual") and now - w["manual"] / 1000 < MANUAL_HOLD.total_seconds()
            pos = self.position(w)
            if pos is None or manual:
                continue
            if hot and lit and "heat" not in w and pos > target + 5:
                if await self._move(w, target, "heat"):
                    w["heat"] = {"from": pos, "to": target}
                    changed = True
            elif "heat" in w and not (hot and lit):
                back = w.pop("heat")
                changed = True
                # only if nobody moved it since
                if abs(pos - back["to"]) <= 3:
                    await self._move(w, back["from"], "heat_end")
        if changed:
            self._save()

    # ---------------------------------------------------------------- events

    def _run(self, job: Callable[[], Any]) -> Callable[..., None]:
        @callback
        def run(*_args: Any) -> None:
            self.hass.async_create_task(job())

        return run

    def _next(self, event: str) -> datetime | None:
        from homeassistant.helpers.sun import get_astral_event_next  # noqa: PLC0415

        try:
            return get_astral_event_next(self.hass, event)
        except ValueError:
            return None

    # ---------------------------------------------------------------- brightness

    def dark_sensor(self) -> str | None:
        """Return the chosen "dark" sensor (else the first camera's one that exists)."""
        if own := self.settings()["dark_sensor"]:
            return own
        found = self.dark_candidates()
        return found[0] if found else None

    def dark_candidates(self) -> list[str]:
        """Return the binary sensors that say whether it is dark."""
        return sorted(
            s.entity_id
            for s in self.hass.states.async_all("binary_sensor")
            if _DARK.search(s.entity_id)
        )

    def is_dark(self) -> bool | None:
        """Return True while the sensor says dark (None without a usable sensor)."""
        sensor = self.dark_sensor()
        state = self.hass.states.get(sensor) if sensor else None
        if state is None or state.state in _GONE:
            return None
        return state.state == "on"

    def morning_at(self, now: datetime) -> datetime:
        """Return today's morning time (weekday or weekend) in local time."""
        local = dt_util.as_local(now)
        s = self.settings()
        value = s["morning_weekend"] if local.weekday() >= 5 else s["morning"]
        hour, minute = _hm(value, "07:00")
        return local.replace(hour=hour, minute=minute, second=0, microsecond=0)

    @callback
    def _dark_check(self, *_args: Any) -> None:
        """Close when it has been dark long enough, open when light (not before the morning)."""
        s = self.settings()
        if s["trigger"] != "dark" or (dark := self.is_dark()) is None:
            return
        sensor = self.hass.states.get(self.dark_sensor() or "")
        delay = timedelta(minutes=float(_num(s["dark_delay"]) or 0))
        now = dt_util.utcnow()
        if sensor is None or now - sensor.last_changed < delay - timedelta(seconds=2):
            return
        # once per dusk and once per morning (a step switched off still counts as done)
        last = self.data.get("dark_last")
        if dark and last != "close":
            self.data["dark_last"] = "close"
            if s["evening_on"]:
                self._start(None, "evening")
        elif not dark and last != "open":
            if now < self.morning_at(now):
                return  # the morning time follows with its own check
            self.data["dark_last"] = "open"
            if s["morning_on"]:
                self._start(100, "morning")

    @callback
    def _dark_changed(self, event: Event) -> None:
        old, new = event.data.get("old_state"), event.data.get("new_state")
        if old is None or new is None or old.state in _GONE or new.state in _GONE:
            return  # a restart or a short outage is no dusk
        if old.state == new.state:
            return
        delay = float(_num(self.settings()["dark_delay"]) or 0) * 60
        for unsub in self._dark:
            unsub()
        self._dark.clear()
        self._dark.append(async_call_later(self.hass, delay, self._dark_check))

    @callback
    def _schedule(self) -> None:
        for unsub in self._sched:
            unsub()
        self._sched.clear()
        s = self.settings()
        if s["trigger"] == "dark":
            if self.data.get("dark_last") is None and (dark := self.is_dark()) is not None:
                # what it is right now counts as done: only the next dusk or morning moves
                self.data["dark_last"] = "close" if dark else "open"
            if sensor := self.dark_sensor():
                self._sched.append(
                    async_track_state_change_event(self.hass, [sensor], self._dark_changed)
                )
            # light before the morning time: open at the morning time
            if s["morning_on"]:
                for value in (s["morning"], s["morning_weekend"]):
                    hour, minute = _hm(value, "07:00")
                    self._sched.append(
                        async_track_time_change(
                            self.hass, self._dark_check, hour=hour, minute=minute, second=5
                        )
                    )
            return
        if s["morning_on"]:
            for value, days in ((s["morning"], "weekday"), (s["morning_weekend"], "weekend")):
                hour, minute = _hm(value, "07:00")

                @callback
                def morning(now: datetime, days: str = days) -> None:
                    weekend = dt_util.as_local(now).weekday() >= 5
                    if weekend == (days == "weekend"):
                        self.hass.async_create_task(self.async_morning())

                self._sched.append(
                    async_track_time_change(self.hass, morning, hour=hour, minute=minute, second=0)
                )
        if s["evening_on"]:

            @callback
            def sunset(*_args: Any) -> None:
                # winter: right at sunset, else some minutes later
                offset = 0 if self.winter() else int(_num(self.settings()["evening_offset"]) or 0)
                if offset <= 0:
                    self._start(None, "evening")
                else:
                    self._sched.append(
                        async_call_later(
                            self.hass, offset * 60, self._run(lambda: self._evening_walk())
                        )
                    )

            self._sched.append(async_track_sunset(self.hass, sunset))

    async def _evening_walk(self) -> None:  # noqa: RUF029
        self._start(None, "evening")

    @callback
    def _changed(self, event: Event) -> None:
        entity_id = event.data["entity_id"]
        new = event.data.get("new_state")
        for w in self.items.values():
            if entity_id == w.get("cover") and new is not None:
                old = event.data.get("old_state")
                moved = old is None or old.attributes.get("current_position") != new.attributes.get(
                    "current_position"
                )
                ours = new.context.id in self._ctx or (
                    new.context.parent_id and new.context.parent_id in self._ctx
                )
                if moved and not ours and new.state in ("opening", "closing", "open", "closed"):
                    w["manual"] = now_ms()
            elif entity_id == w.get("contact") and new is not None and new.state == "off":
                # door shut again: catch up on the evening close
                if w.get("pending") == "evening":
                    self.hass.async_create_task(self._move(w, None, "evening"))
        # forget old contexts
        cutoff = dt_util.utcnow().timestamp() - 600
        self._ctx = {k: v for k, v in self._ctx.items() if v >= cutoff}

    @callback
    def _registry(self, event: Event) -> None:
        if event.data.get("action") == "create" and str(event.data.get("entity_id", "")).startswith(
            "cover."
        ):
            # the state comes a moment later
            self.hass.loop.call_later(5, self._discover_soon)

    @callback
    def _discover_soon(self) -> None:
        if self.discover():
            _LOGGER.info("New covers became windows")

    @callback
    def async_setup(self) -> None:
        """Find the windows, start the automations."""
        self.discover()
        self._schedule()
        self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, self._changed))
        self._unsub.append(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, self._registry)
        )
        self._unsub.append(async_track_time_interval(self.hass, self.async_heat, HEAT_INTERVAL))

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in [*self._unsub, *self._sched, *self._dark]:
            unsub()
        self._unsub.clear()
        self._sched.clear()
        self._dark.clear()
        if self._seq is not None and not self._seq.done():
            self._seq.cancel()

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return windows and settings for the dashboard."""
        azimuth, elevation = self.sun()
        out = []
        for wid, w in self.items.items():
            out.append(
                {
                    "id": wid,
                    **{k: w.get(k) for k in ("name", "area_id", "cover", "contact", "lock")},
                    "door": bool(w.get("door")),
                    "orientation": w.get("orientation"),
                    "auto": w.get("auto", True),
                    "cleaned": self.last_cleaned(w),
                    "clean_due": self.clean_due(w),
                    "open": self.is_open(w),
                    "position": self.position(w),
                    "sun": facade_lit(w.get("orientation"), azimuth, elevation),
                    "heat": bool(w.get("heat")),
                    "pending": w.get("pending"),
                    "last": w.get("last"),
                }
            )
        return {
            "items": out,
            "settings": self.settings(),
            "winter": self.winter(),
            "outdoor": self.outdoor(),
            "dark": self.is_dark(),
            "dark_sensor": self.dark_sensor(),
            "dark_candidates": self.dark_candidates(),
            "last_run": self.data.get("last_run"),
        }


# ---------------------------------------------------------------- entities


class WindowEntity(Entity):
    """An entity of a window; every window is a device of its own (in its area)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, windows: Windows, wid: str, key: str, domain: str) -> None:
        """Initialize (entity ID: <domain>.window_<id>_<key>)."""
        self._windows = windows
        self._wid = wid
        item = windows.items[wid]
        self._attr_unique_id = f"{entry_id}_window_{wid}_{key}"
        self._attr_translation_key = f"window_{key}"
        self.entity_id = f"{domain}.window_{wid}_{key}"
        area = None
        if item.get("area_id"):
            entry = ar.async_get(windows.hass).async_get_area(item["area_id"])
            area = entry.name if entry else None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry_id}_window_{wid}")},
            name=item.get("name") or wid,
            manufacturer="Smarthome UI",
            model="Fenster",
            suggested_area=area,
        )

    @property
    def item(self) -> dict[str, Any]:
        """Return the window (empty once removed)."""
        return self._windows.items.get(self._wid) or {}

    @property
    def available(self) -> bool:
        """Return False once the window was removed."""
        return self._wid in self._windows.items

    async def async_added_to_hass(self) -> None:
        """Update when the windows change."""
        for signal in (SIGNAL_WINDOWS, SIGNAL_DATA):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self.async_write_ha_state)
            )


@callback
def async_setup_window_entities(
    hass: HomeAssistant, windows: Windows, add: Any, create: Any
) -> CALLBACK_TYPE:
    """Add the entities of every window (and of windows that appear later)."""
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = [wid for wid in windows.items if wid not in known]
        known.update(new)
        if entities := [e for wid in new for e in create(wid)]:
            add(entities)

    add_new()
    return async_dispatcher_connect(hass, SIGNAL_WINDOWS, add_new)
