"""Plants (``plant.*``, e.g. the Plant Monitor integration): watering, forecast, rain, sensors.

Core attaches its own entities to the device of each plant:

* **watered** – a clear rise of the soil moisture (``WATER_JUMP`` points within an hour) counts as
  watered; the button "Gegossen" (or checking the task off) does the same by hand,
* **next watering** – from how fast the soil dried since then, when the lower limit is reached,
* the tasks go to the right list: plants in a garden, on a balcony or terrace → "Garten"
  (``gd.plants``), all others → "Haushalt" (``hh.plants``),
* **rain**: outdoor plants need no watering if it rained in the last 12 hours or rain is coming
  in the next 6 hours,
* **sensor offline**: no value for 24 hours → "Sensor <plant> prüfen",
* **fertilizing by calendar** for plants without fertilizer sensor: every N weeks between two
  months (settings from the dashboard, ``plants/settings``).

Per plant in ``plants``: ``{"watered": ms, "samples": [[ms, moisture], ...]}``; done records
``plant:<entity>|watered`` (by hand) and ``plant:<entity>|fed`` (fertilized by calendar).
"""

from __future__ import annotations

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
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util, slugify

from .chores import start_of_day
from .const import SIGNAL_DATA, SIGNAL_PLANTS, SIGNAL_TASKS, WATCH_INTERVAL
from .store import CoreStore, now_ms

HOUR_MS = 3_600_000
DAY_MS = 86_400_000
WATER_JUMP = 10  # moisture points within JUMP_MINUTES = watered
JUMP_MINUTES = 60
SAMPLE_MINUTES = 25
KEEP_DAYS = 7
MIN_SPAN_HOURS = 12  # forecast only with at least this much drying
OFFLINE_HOURS = 24
RAIN_PAST_HOURS = 12
RAIN_NEXT_HOURS = 6
DEFAULT_MIN_MOISTURE = 20
RAINY = ("rainy", "pouring", "lightning-rainy", "snowy-rainy", "hail")
INVALID = ("unknown", "unavailable", "")
OUTDOOR = re.compile(
    r"(garten|garden|balkon|balcony|terrasse|terrace|außen|aussen|outdoor|hof|patio|beet)",
    re.IGNORECASE,
)
_MOISTURE = re.compile(r"(soil|moisture|bodenfeuchte|feuchte)")
_NOT_MOISTURE = re.compile(r"(humid|luftfeuchte|air|min|max)")
_MIN_MOISTURE = re.compile(r"min.*(moisture|feuchte)|(moisture|feuchte).*min")
_CONDUCTIVITY = re.compile(r"(conductivity|leitfähig|leitfaehig|fertil|dünger|duenger)")
_PROBLEM = re.compile(r"(moisture|conductivity) (low|high)")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _day(ms: int) -> str:
    return dt_util.as_local(dt_util.utc_from_timestamp(ms / 1000)).date().isoformat()


def in_months(month: int, start: int, end: int) -> bool:
    """Return True if a month lies between start and end (both included, may wrap)."""
    return start <= month <= end if start <= end else month >= start or month <= end


class PlantTracker:
    """One plant entity."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, entity_id: str) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.store = store
        self.entity_id = entity_id
        self._recent: list[tuple[int, float]] = []

    # ---------------------------------------------------------------- entity, device, area

    @property
    def state(self) -> State | None:
        """Return the plant state."""
        return self.hass.states.get(self.entity_id)

    @property
    def name(self) -> str:
        """Return the plant name."""
        state = self.state
        return (
            str(state.attributes.get("friendly_name") or self.entity_id)
            if state
            else self.entity_id
        )

    def _entry(self) -> er.RegistryEntry | None:
        return er.async_get(self.hass).async_get(self.entity_id)

    @property
    def device_id(self) -> str | None:
        """Return the plant's device (Plant Monitor creates one per plant)."""
        entry = self._entry()
        return entry.device_id if entry else None

    @property
    def area_id(self) -> str | None:
        """Return the area of the plant (entity, else device)."""
        entry = self._entry()
        if entry and entry.area_id:
            return entry.area_id
        device = dr.async_get(self.hass).async_get(self.device_id) if self.device_id else None
        return device.area_id if device else None

    @property
    def outdoor(self) -> bool:
        """Return True for plants in a garden, on a balcony or terrace (or without area)."""
        area = ar.async_get(self.hass).async_get_area(self.area_id) if self.area_id else None
        return area is None or bool(OUTDOOR.search(f"{area.id} {area.name}"))

    @property
    def source(self) -> str:
        """Return the notification source (and so the to-do list)."""
        return "gd.plants" if self.outdoor else "hh.plants"

    def device_info(self) -> DeviceInfo | None:
        """Return the device our entities belong to."""
        device = dr.async_get(self.hass).async_get(self.device_id) if self.device_id else None
        return DeviceInfo(identifiers=set(device.identifiers)) if device else None

    def _device_entities(self) -> list[er.RegistryEntry]:
        if not self.device_id:
            return []
        return [
            e
            for e in er.async_entries_for_device(er.async_get(self.hass), self.device_id)
            if e.platform != "smarthome_ui"
        ]

    def _text(self, entry: er.RegistryEntry) -> str:
        state = self.hass.states.get(entry.entity_id)
        name = state.attributes.get("friendly_name", "") if state else ""
        return f"{entry.translation_key or ''} {entry.entity_id} {name}".lower()

    @property
    def moisture_entity(self) -> str | None:
        """Return the soil moisture sensor of the plant's device."""
        best = None
        for entry in self._device_entities():
            if entry.domain != "sensor":
                continue
            state = self.hass.states.get(entry.entity_id)
            text = self._text(entry)
            if state is not None and state.attributes.get("device_class") == "moisture":
                return entry.entity_id
            if best is None and _MOISTURE.search(text) and not _NOT_MOISTURE.search(text):
                best = entry.entity_id
        return best

    @property
    def has_conductivity(self) -> bool:
        """Return True if the plant has a fertilizer (conductivity) sensor."""
        if self.state is not None and "conductivity" in self.state.attributes:
            return True
        return any(
            e.domain == "sensor" and _CONDUCTIVITY.search(self._text(e))
            for e in self._device_entities()
        )

    def moisture(self) -> float | None:
        """Return the soil moisture (sensor, else plant attribute)."""
        state = self.hass.states.get(self.moisture_entity or "")
        if state is not None and state.state not in INVALID:
            return _num(state.state)
        return _num(self.state.attributes.get("moisture")) if self.state else None

    def min_moisture(self) -> float:
        """Return the lower moisture limit (Plant Monitor threshold, else 20 %)."""
        for entry in self._device_entities():
            if entry.domain in ("number", "sensor") and _MIN_MOISTURE.search(self._text(entry)):
                state = self.hass.states.get(entry.entity_id)
                if state is not None and (value := _num(state.state)) is not None:
                    return value
        attrs = self.state.attributes if self.state else {}
        for key in ("min_moisture", "min_soil_moisture"):
            if (value := _num(attrs.get(key))) is not None:
                return value
        return DEFAULT_MIN_MOISTURE

    def source_entity(self) -> str | None:
        """Return the entity that really reports (Plant Monitor mirrors an external sensor)."""
        moisture = self.moisture_entity
        state = self.hass.states.get(moisture or "")
        external = state.attributes.get("external_sensor") if state else None
        if external and self.hass.states.get(external) is not None:
            return str(external)
        return moisture

    # ---------------------------------------------------------------- data

    def _data(self) -> dict[str, Any]:
        return self.store.data["plants"].setdefault(self.entity_id, {})

    def settings(self) -> dict[str, Any]:
        """Return the own settings (fertilizing by calendar)."""
        return self.store.data["plant_settings"].get(self.entity_id) or {}

    def last_watered(self) -> tuple[int | None, str | None]:
        """Return when it was last watered and how it was noticed (auto / hand)."""
        auto = int(self._data().get("watered") or 0)
        hand = int((self.store.done.get(f"plant:{self.entity_id}|watered") or {}).get("t") or 0)
        if not auto and not hand:
            return None, None
        return (hand, "hand") if hand >= auto else (auto, "auto")

    # ---------------------------------------------------------------- moisture, watering, forecast

    @callback
    def update(self, now: int) -> bool:
        """Take the current moisture: notice watering, keep samples; True if watered now."""
        value = self.moisture()
        if value is None:
            return False
        self._recent = [(t, v) for t, v in self._recent if now - t <= JUMP_MINUTES * 60_000]
        self._recent.append((now, value))
        data = self._data()
        samples: list[list[float]] = data.setdefault("samples", [])
        watered = False
        low = min(v for _, v in self._recent)
        last, _by = self.last_watered()
        if value - low >= WATER_JUMP and (not last or now - last > 2 * HOUR_MS):
            data["watered"] = now
            self._recent = [(now, value)]
            watered = True
        if (
            not samples
            or now - samples[-1][0] >= SAMPLE_MINUTES * 60_000
            or abs(samples[-1][1] - value) >= 1
        ):
            samples.append([now, value])
            del samples[: max(0, len(samples) - 2000)]
            cut = now - KEEP_DAYS * DAY_MS
            samples[:] = [s for s in samples if s[0] >= cut]
            self.store.save()
        return watered

    def drying_rate(self, now: int | None = None) -> float | None:
        """Return the moisture change per day since the last watering (negative = drying)."""
        now = now or now_ms()
        last, _by = self.last_watered()
        start = max((last or 0) + HOUR_MS, now - 4 * DAY_MS)
        points = [(t, v) for t, v in self._data().get("samples") or [] if t >= start]
        if len(points) < 4 or (points[-1][0] - points[0][0]) < MIN_SPAN_HOURS * HOUR_MS:
            return None
        n = len(points)
        mean_t = sum(t for t, _ in points) / n
        mean_v = sum(v for _, v in points) / n
        var = sum((t - mean_t) ** 2 for t, _ in points)
        if not var:
            return None
        slope = sum((t - mean_t) * (v - mean_v) for t, v in points) / var
        return slope * DAY_MS

    def next_watering(self, now: int | None = None) -> int | None:
        """Return when the soil reaches the lower limit (now if it is already there)."""
        now = now or now_ms()
        if self.needs_water():
            # watered after the plant reported "low": wait until the plant reports again
            last, _by = self.last_watered()
            state = self.state
            if last and state is not None and last > _ms(state.last_changed):
                return None
            return now
        value = self.moisture()
        if value is None:
            return None
        limit = self.min_moisture()
        if value <= limit:
            last, _by = self.last_watered()
            return None if last and now - last < 6 * HOUR_MS else now
        rate = self.drying_rate(now)
        if rate is None or rate > -0.5:
            return None
        return int(now + (value - limit) / -rate * DAY_MS)

    def needs_water(self) -> bool:
        """Return True if the plant reports moisture low."""
        return "water" in self.needs()

    def needs(self) -> set[str]:
        """Return "water" / "feed" from the plant's status attributes."""
        state = self.state
        if state is None:
            return set()
        attrs = state.attributes
        needs = set()
        if str(attrs.get("moisture_status", "")).lower() == "low":
            needs.add("water")
        if str(attrs.get("conductivity_status", "")).lower() == "low":
            needs.add("feed")
        for kind, level in _PROBLEM.findall(str(attrs.get("problem", "")).lower()):
            if level == "low":
                needs.add("water" if kind == "moisture" else "feed")
        return needs

    def offline_since(self, now: int | None = None) -> int | None:
        """Return since when the sensor sends nothing (None while it reports)."""
        now = now or now_ms()
        source = self.source_entity()
        state = self.hass.states.get(source or "")
        if state is None:
            return None
        if state.state in INVALID:
            since = _ms(state.last_changed)
        else:
            since = _ms(getattr(state, "last_reported", None) or state.last_updated)
        return since if now - since >= OFFLINE_HOURS * HOUR_MS else None

    # ---------------------------------------------------------------- fertilizing by calendar

    def feed_due(self, now: int | None = None) -> int | None:
        """Return when to fertilize by calendar (None if off or outside the months)."""
        cfg = self.settings().get("feed") or {}
        weeks = int(cfg.get("every") or 0)
        if weeks <= 0:
            return None
        start_m, end_m = int(cfg.get("from") or 3), int(cfg.get("to") or 9)
        now = now or now_ms()
        local = dt_util.as_local(dt_util.utc_from_timestamp(now / 1000))
        if not in_months(local.month, start_m, end_m):
            return None
        # start of the current season (may be last year if the months wrap)
        year = local.year if local.month >= start_m else local.year - 1
        season = local.replace(
            year=year if start_m > end_m else local.year,
            month=start_m,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        last = int((self.store.done.get(f"plant:{self.entity_id}|fed") or {}).get("t") or 0)
        if last < _ms(season):
            return _ms(season)
        return start_of_day(last + weeks * 7 * DAY_MS)

    # ---------------------------------------------------------------- tasks, dashboard

    def tasks(self, rain: str | None, now: int | None = None) -> list[dict[str, Any]]:
        """Return watering, fertilizing and sensor tasks of the plant."""
        now = now or now_ms()
        src = self.source
        icon_water, icon_feed = "mdi:watering-can-outline", "mdi:sprout-outline"
        last, _by = self.last_watered()
        out = []
        nxt = self.next_watering(now)
        if nxt is not None and not (self.outdoor and rain):
            cycle = _day(last) if last else "0"
            out.append(
                {
                    "key": f"plant:{self.entity_id}|water|{cycle}",
                    "name": f"{self.name} gießen",
                    "src": src,
                    "area": None,
                    "icon": icon_water,
                    "interval": None,
                    "last": last,
                    "due": nxt,
                }
            )
        state = self.state
        if "feed" in self.needs() and state is not None:
            out.append(
                {
                    "key": f"plant:{self.entity_id}|feed|{_day(_ms(state.last_changed))}",
                    "name": f"{self.name} düngen",
                    "src": src,
                    "area": None,
                    "icon": icon_feed,
                    "interval": None,
                    "last": None,
                    "due": _ms(state.last_changed),
                }
            )
        elif (feed := self.feed_due(now)) is not None:
            fed = (self.store.done.get(f"plant:{self.entity_id}|fed") or {}).get("t")
            weeks = int((self.settings().get("feed") or {}).get("every") or 0)
            out.append(
                {
                    "key": f"plant:{self.entity_id}|fed",
                    "name": f"{self.name} düngen",
                    "src": src,
                    "area": None,
                    "icon": icon_feed,
                    "interval": weeks * 7,
                    "last": fed or None,
                    "due": feed,
                }
            )
        if (since := self.offline_since(now)) is not None:
            out.append(
                {
                    "key": f"plant:{self.entity_id}|offline|{_day(since)}",
                    "name": f"Sensor {self.name} prüfen – keine Werte seit {_day(since)}",
                    "src": src,
                    "area": None,
                    "icon": "mdi:access-point-network-off",
                    "interval": None,
                    "last": None,
                    "due": since,
                }
            )
        return out

    def info(self, rain: str | None) -> dict[str, Any]:
        """Return what the dashboard needs."""
        last, by = self.last_watered()
        rate = self.drying_rate()
        return {
            "entity": self.entity_id,
            "name": self.name,
            "outdoor": self.outdoor,
            "watered": last,
            "watered_by": by,
            "next_watering": self.next_watering(),
            "rate": round(rate, 1) if rate is not None else None,
            "min_moisture": self.min_moisture(),
            "rain": rain if self.outdoor else None,
            "offline": self.offline_since(),
            "conductivity": self.has_conductivity,
            "feed": dict(self.settings().get("feed") or {}),
            "feed_due": self.feed_due(),
            "fed": (self.store.done.get(f"plant:{self.entity_id}|fed") or {}).get("t") or None,
        }

    @callback
    def mark_watered(self, who: str | None = None) -> None:
        """Watered by hand now."""
        self.store.mark_done(
            f"plant:{self.entity_id}|watered", who=who, label=f"{self.name} gegossen"
        )


class PlantManager:
    """All plants: watering, rain and the tasks."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, watchers: Any) -> None:
        """Initialize the manager (the watchers read the hourly forecast)."""
        self.hass = hass
        self.store = store
        self.watchers = watchers
        self.plants: dict[str, PlantTracker] = {}
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_setup(self) -> None:
        """Find the plants and follow their moisture sensors."""
        self._discover()

        @callback
        def is_relevant(data: EventStateChangedData) -> bool:
            eid = data["entity_id"]
            return eid.startswith("plant.") or eid in self._sensors

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            eid = event.data["entity_id"]
            if eid.startswith("plant.") and eid not in self.plants:
                self._discover()
            plant = self.plants.get(eid) or self._sensors.get(eid)
            if plant is not None:
                self._update(plant)

        self._sensors: dict[str, PlantTracker] = {}
        self._index()
        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=is_relevant)
        )
        self._unsub.append(async_track_time_interval(self.hass, self._tick, WATCH_INTERVAL))
        self._tick()

    @callback
    def _discover(self) -> None:
        new = False
        for state in self.hass.states.async_all("plant"):
            if state.entity_id not in self.plants:
                self.plants[state.entity_id] = PlantTracker(self.hass, self.store, state.entity_id)
                new = True
        if new:
            if hasattr(self, "_sensors"):
                self._index()
            async_dispatcher_send(self.hass, SIGNAL_PLANTS)

    @callback
    def _index(self) -> None:
        self._sensors = {
            sensor: plant
            for plant in self.plants.values()
            if (sensor := plant.moisture_entity) is not None
        }

    @callback
    def _update(self, plant: PlantTracker) -> None:
        if plant.update(now_ms()):
            self.store.save(SIGNAL_DATA, SIGNAL_TASKS)
        else:
            async_dispatcher_send(self.hass, SIGNAL_PLANTS)

    @callback
    def _tick(self, _now: Any = None) -> None:
        self._discover()
        self._note_rain()
        for plant in self.plants.values():
            self._update(plant)
        async_dispatcher_send(self.hass, SIGNAL_TASKS)

    @callback
    def async_stop(self) -> None:
        """Stop following."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    # ---------------------------------------------------------------- rain

    @callback
    def _note_rain(self) -> None:
        """Remember when it last rained (weather, rain sensors, precipitation sensors)."""
        now = now_ms()
        raining = any(s.state in RAINY for s in self.hass.states.async_all("weather"))
        for state in self.hass.states.async_all("binary_sensor"):
            eid = state.entity_id
            if re.search(r"(rain|regen)", eid) and state.attributes.get("device_class") in (
                None,
                "moisture",
            ):
                if state.state == "on":
                    raining = True
        for state in self.hass.states.async_all("sensor"):
            if state.attributes.get("device_class") == "precipitation_intensity":
                if (_num(state.state) or 0) > 0:
                    raining = True
        if raining:
            self.store.data["plant_rain"] = now

    def rain(self) -> str | None:
        """Return "rained" / "coming" if outdoor plants need no water now."""
        now = now_ms()
        if now - int(self.store.data.get("plant_rain") or 0) <= RAIN_PAST_HOURS * HOUR_MS:
            return "rained"
        end = dt_util.utcnow() + timedelta(hours=RAIN_NEXT_HOURS)
        for item in getattr(self.watchers, "hourly", None) or []:
            when = dt_util.parse_datetime(str(item.get("datetime", "")))
            if when is None or when > end or when < dt_util.utcnow() - timedelta(hours=1):
                continue
            prob = _num(item.get("precipitation_probability"))
            amount = _num(item.get("precipitation")) or 0
            if (item.get("condition") in RAINY or amount >= 1) and (prob is None or prob >= 60):
                return "coming"
        return None

    # ---------------------------------------------------------------- tasks, dashboard

    def tasks(self) -> list[dict[str, Any]]:
        """Return the tasks of all plants."""
        rain = self.rain()
        return [task for plant in self.plants.values() for task in plant.tasks(rain)]

    def info(self) -> list[dict[str, Any]]:
        """Return the plants for the dashboard."""
        rain = self.rain()
        return [plant.info(rain) for plant in self.plants.values()]

    @callback
    def complete(self, key: str, who: str | None = None) -> bool:
        """A watering task checked off = watered by hand; False for other keys."""
        if not key.startswith("plant:") or "|water|" not in key:
            return False
        entity = key.removeprefix("plant:").split("|", 1)[0]
        if (plant := self.plants.get(entity)) is None:
            return False
        plant.mark_watered(who)
        return True

    @callback
    def set_settings(self, entity_id: str, feed: dict[str, Any] | None) -> None:
        """Change the fertilizing calendar of a plant (None = off)."""
        settings = self.store.data["plant_settings"]
        cur = dict(settings.get(entity_id) or {})
        if feed and int(feed.get("every") or 0) > 0:
            cur["feed"] = {
                "every": int(feed["every"]),
                "from": int(feed.get("from") or 3),
                "to": int(feed.get("to") or 9),
            }
        else:
            cur.pop("feed", None)
        settings[entity_id] = cur
        self.store.save(SIGNAL_DATA, SIGNAL_TASKS)


class PlantEntity(Entity):
    """An entity of a plant; belongs to the plant's device (Plant Monitor)."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, plant: PlantTracker, key: str, domain: str) -> None:
        """Initialize (entity ID: <domain>.<garden|household>_<plant>_<key>)."""
        self._plant = plant
        prefix = "garden" if plant.outdoor else "household"
        slug = slugify(plant.entity_id.split(".", 1)[1])
        self._attr_unique_id = f"{entry_id}_plant_{plant.entity_id}_{key}"
        self._attr_translation_key = f"plant_{key}"
        self.entity_id = f"{domain}.{prefix}_{slug}_{key}"
        if info := plant.device_info():
            self._attr_device_info = info
        else:  # no device: the plant's name goes into the entity name
            label = {"last_watered": "zuletzt gegossen", "next_watering": "nächstes Gießen"}
            self._attr_name = f"{plant.name} {label.get(key, 'gegossen')}"

    async def async_added_to_hass(self) -> None:
        """Update with new moisture values and checked off tasks."""
        for signal in (SIGNAL_PLANTS, SIGNAL_DATA):
            self.async_on_remove(
                async_dispatcher_connect(self.hass, signal, self.async_write_ha_state)
            )


@callback
def async_setup_plant_entities(
    hass: HomeAssistant,
    manager: PlantManager,
    add: Any,
    create: Any,
) -> CALLBACK_TYPE:
    """Add the entities of every plant (and of plants that appear later)."""
    known: set[str] = set()

    @callback
    def add_new() -> None:
        new = [p for eid, p in manager.plants.items() if eid not in known]
        for plant in new:
            known.add(plant.entity_id)
        if entities := [e for p in new for e in create(p)]:
            add(entities)

    add_new()
    return async_dispatcher_connect(hass, SIGNAL_PLANTS, add_new)
