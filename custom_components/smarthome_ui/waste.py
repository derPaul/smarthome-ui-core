"""Waste collection: read the waste calendars, tell what is picked up when, remind the day before.

Settings (from the dashboard, ``waste/settings``): ``calendars`` (calendar entities; empty = every
calendar whose name sounds like waste), ``names`` (own names by keyword) and ``remind``
(``{"d": days before, "t": "HH:MM"}``, default the day before at 12:00).

The reminder runs from that time until 10:00 on the pickup day (or the end of that day if the
reminder itself is later) – unless "put out" was tapped (``done["waste:<YYYY-MM-DD>"]``).
"""

from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
import logging
import re
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SIGNAL_TASKS, SIGNAL_WASTE, WASTE_INTERVAL
from .store import CoreStore

_LOGGER = logging.getLogger(__name__)

CALENDAR_RE = re.compile(r"(müll|muell|abfall|abfuhr|waste|garbage|trash|tonne|entsorg|recycl)")
# green waste before "Restmüll" – it often contains "abfall"
KINDS: list[tuple[re.Pattern[str], str, str, str]] = [
    (
        re.compile(
            r"(grün[\s-]*abf|gruen[\s-]*abf|grünschnitt|gruenschnitt|grüngut|gruengut|grünzeug|"
            r"garten[\s-]*abf|gartenabfäll|baum\s*schnitt|baumschnitt|strauchschnitt|laub|weihnacht|"
            r"tannenbaum|green waste|garden waste)"
        ),
        "Grünschnitt",
        "#7ddc1f",
        "mdi:pine-tree",
    ),
    (re.compile(r"(bio|kompost|grüne tonne|gruene tonne)"), "Biotonne", "#b07a3c", "mdi:leaf"),
    (
        re.compile(r"(gelb|leichtverpack|lvp|wertstoff|verpackung)"),
        "Gelbe Tonne",
        "#ffc83d",
        "mdi:recycle",
    ),
    (
        re.compile(r"(papier|pappe|ppk|blau|karton)"),
        "Papier",
        "#2fb8ff",
        "mdi:newspaper-variant-outline",
    ),
    (re.compile(r"(sperr)"), "Sperrmüll", "#ff8a1f", "mdi:sofa-outline"),
    (
        re.compile(r"(schadstoff|problemstoff|sondermüll|sondermuell)"),
        "Schadstoffe",
        "#ff3d7f",
        "mdi:biohazard",
    ),
    (
        re.compile(r"(rest|hausmüll|hausmuell|schwarz|grau|müll|muell|abfall|tonne)"),
        "Restmüll",
        "#8a8d99",
        "mdi:trash-can-outline",
    ),
]
DAYS_AHEAD = 45


def classify(text: str, names: dict[str, str]) -> dict[str, str]:
    """Return name, color and icon of a calendar entry ("Restabfall" → Restmüll)."""
    low = str(text or "").lower()
    kind = next((k for k in KINDS if k[0].search(low)), None)
    own = next((v for k, v in names.items() if k.lower() in low), None)
    name = kind[1] if kind else text
    if name == "Grünschnitt":
        name = (
            "Grünabfall"
            if "abf" in low
            else "Grüngut"
            if re.search(r"(grüngut|gruengut)", low)
            else name
        )
    return {
        "name": own or name,
        "color": kind[2] if kind else "#8a8d99",
        "icon": kind[3] if kind else "mdi:trash-can-outline",
    }


class WasteManager:
    """Pickup days from the calendars, reminder, to-do item."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self.days: list[dict[str, Any]] = []  # [{"date": date, "kinds": [{name, color, icon}]}]
        self._unsub: list[CALLBACK_TYPE] = []

    @property
    def settings(self) -> dict[str, Any]:
        """Return the waste settings."""
        return self.store.data["waste"]

    @property
    def configured(self) -> bool:
        """Return True once the dashboard handed over the settings."""
        return bool(self.settings.get("configured"))

    @callback
    def async_setup(self) -> None:
        """Read the calendars now and every hour."""
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_refresh, WASTE_INTERVAL)
        )
        self.hass.async_create_task(self._async_refresh())

    @callback
    def async_stop(self) -> None:
        """Stop reading."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def calendars(self) -> list[str]:
        """Return the waste calendars (set ones, else found by name)."""
        own = [c for c in self.settings.get("calendars") or [] if self.hass.states.get(c)]
        if own:
            return own
        return [
            s.entity_id
            for s in self.hass.states.async_all("calendar")
            if CALENDAR_RE.search(f"{s.entity_id} {s.attributes.get('friendly_name', '')}".lower())
        ]

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Take the settings from the dashboard."""
        self.store.data["waste"] = {**self.settings, **settings, "configured": True}
        self.store.save()
        self.hass.async_create_task(self._async_refresh())

    async def _async_refresh(self, _now: Any = None) -> None:
        cals = self.calendars()
        if not self.configured or not cals:
            self.days = []
            self._changed()
            return
        start = dt_util.start_of_local_day()
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {"start_date_time": start, "end_date_time": start + timedelta(days=DAYS_AHEAD)},
                target={"entity_id": cals},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("Waste calendars not available: %s", err)
            return
        names = self.settings.get("names") or {}
        by_day: dict[date, list[dict[str, str]]] = {}
        for cal in cals:
            for event in (response or {}).get(cal, {}).get("events", []):
                text = str(event.get("start", ""))
                day = dt_util.parse_date(text[:10])
                if day is None or day < start.date():
                    continue
                kind = classify(event.get("summary") or event.get("description") or "", names)
                kinds = by_day.setdefault(day, [])
                if all(k["name"] != kind["name"] for k in kinds):
                    kinds.append(kind)
        self.days = [{"date": d, "kinds": by_day[d]} for d in sorted(by_day)]
        self._changed()

    def _changed(self) -> None:
        async_dispatcher_send(self.hass, SIGNAL_WASTE)
        async_dispatcher_send(self.hass, SIGNAL_TASKS)

    # ---------------------------------------------------------------- reminder

    def remind_at(self, day: date) -> datetime:
        """Return when the reminder for a pickup day starts."""
        remind = self.settings.get("remind") or {}
        hour, minute = (str(remind.get("t") or "12:00").split(":") + ["0"])[:2]
        moment = datetime.combine(
            day - timedelta(days=int(remind.get("d", 1) or 0)),
            dtime(int(hour), int(minute)),
            dt_util.get_default_time_zone(),
        )
        return moment

    def remind_until(self, day: date) -> datetime:
        """Return when the reminder ends (10:00 on the pickup day, later if it starts later)."""
        tz = dt_util.get_default_time_zone()
        ten = datetime.combine(day, dtime(10, 0), tz)
        return ten if self.remind_at(day) < ten else datetime.combine(day, dtime(23, 59, 59), tz)

    def key(self, day: date) -> str:
        """Return the done key of a pickup day."""
        return f"waste:{day.isoformat()}"

    def done(self, day: date) -> bool:
        """Return True if "put out" (or "not this time") was tapped."""
        return bool((self.store.done.get(self.key(day)) or {}).get("t"))

    def current(self) -> dict[str, Any] | None:
        """Return the pickup day whose reminder is running right now (not yet put out)."""
        now = dt_util.now()
        for entry in self.days[:3]:
            day = entry["date"]
            if self.remind_at(day) <= now < self.remind_until(day) and not self.done(day):
                return entry
        return None

    def label(self, entry: dict[str, Any], at: datetime) -> str:
        """Return "Morgen: Restmüll, Papier rausstellen" relative to a moment."""
        diff = (entry["date"] - at.date()).days
        when = "Heute" if diff == 0 else "Morgen" if diff == 1 else f"{entry['date']:%d.%m.}"
        return f"{when}: {', '.join(k['name'] for k in entry['kinds'])} rausstellen"

    def tasks(self) -> list[dict[str, Any]]:
        """Return the running reminder as task (household to-do list)."""
        entry = self.current()
        if entry is None:
            return []
        at = self.remind_at(entry["date"])
        return [
            {
                "key": self.key(entry["date"]),
                "name": self.label(entry, dt_util.now()),
                "src": "hh.waste",
                "area": None,
                "icon": entry["kinds"][0]["icon"],
                "interval": None,
                "last": None,
                "due": int(at.timestamp() * 1000),
            }
        ]

    def rules(self) -> list[dict[str, Any]]:
        """Return the reminder notifications of the next pickup days ("at" rules)."""
        cfg = (self.store.data["notify"].get("sources") or {}).get("hh.waste") or {}
        if not self.configured or not cfg.get("push"):
            return []
        now = dt_util.now()
        out = []
        for entry in self.days[:4]:
            at = self.remind_at(entry["date"])
            if at + timedelta(hours=6) < now:
                continue
            out.append(
                {
                    "id": f"waste|{entry['date'].isoformat()}",
                    "type": "at",
                    "at": int(at.timestamp() * 1000),
                    "skip_if_done": self.key(entry["date"]),
                    "src": "hh.waste",
                    "title": "Müllabfuhr",
                    "urgent": bool(cfg.get("urgent")),
                    "url": cfg.get("url") or "/",
                    "message": self.label(entry, at),
                }
            )
        return out
