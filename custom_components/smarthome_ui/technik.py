"""House technology: heat pump, inverter, heating pressure, water softener, water and base load.

Which sensors count comes from the dashboard (``technik/settings``), the way its Haustechnik page
found or was told them. Core checks every minute and notifies (sources "Heizung & Wärmepumpe"
``ht.heating``, "Strom, PV & Batterie" ``ht.energy``, "Wasser & Enthärter" ``ht.water``):

* **faults** – heat pump / inverter error state, a device not reachable for 10 minutes,
* **heating pressure** – below ``pressure_min`` (1.5 bar): "Heizungswasser nachfüllen", urgent below
  ``pressure_critical``; falling by ``pressure_drop`` bar per week or more (daily minimum, 3 weeks),
* **water softener** – salt at 20 % of ``salt_max`` or less, alarms / warnings / notes of the
  decalcifier (SYR …),
* **water watch** – water flows for ``water_minutes`` (40) without a break, at night (1–5 h) after
  10 minutes: urgent, with the action "Wasser absperren" if a main valve is found,
* **cycling** – the compressor starts more than ``cycles_per_hour`` times per hour (6 hours),
* **base load** – the lowest house consumption between 0 and 5 o'clock is clearly above the usual
  night (median of the last 15 nights, at least 5).

Entities (device "Haustechnik"): "Hinweise" (sensor), the watches as switches, minimum pressure
and water minutes as numbers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
import re
from statistics import median
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED, EntityCategory
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SIGNAL_DATA, SIGNAL_TECHNIK
from .entity import DEVICE_TECHNIK, CoreEntity
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

ACTION_VALVE = "SH_WASSER_ZU"
UNREACHABLE_MIN = 10
FLOW_ON = 0.05
NIGHT = (1, 5)
DAYS_KEEP = 21
NIGHTS_KEEP = 15
DEFAULTS: dict[str, Any] = {
    "heatpump_error": None,
    "inverter_error": None,
    "inverter_error_ok": None,
    "reach": {},  # {"Wechselrichter": entity, "Batterie": entity, "Heizkreis": entity}
    "pressure": None,
    "pressure_min": 1.5,
    "pressure_critical": 1.0,
    "pressure_drop": 0.08,
    "salt": None,
    "salt_max": 25,
    "syr": {},  # {"Alarm": entity, "Warnung": entity, "Hinweis": entity}
    "flow": None,
    "valve": None,
    "water_minutes": 40,
    "compressor": None,
    "cycles_per_hour": 4,
    "power": {},  # {"house"} or {"pv", "grid", "battery", "grid_invert", "battery_invert"}
    # forecasts: battery {power, soc, capacity (kWh), invert, reserve (%), max_discharge}, hot water
    "battery": {},
    "dhw": None,
    "dhw_target": None,
    "hp_power": None,  # what the heat pump draws (W)
    "heat_mode": None,  # heating mode on / off (cycles)
    "summer_mode": None,
    "checks": {"pressure": True, "water": True, "cycling": True, "baseload": True},
}
_BENIGN = re.compile(
    r"(zu geringe dc|dc.?leistung.*gering|low dc|insufficient dc|schlaf|sleep|standby|nacht|night|"
    r"keine sonne|no sun)",
    re.IGNORECASE,
)
_NO_ERROR = re.compile(
    r"^(ok|normal|none|no|no_error|no error|kein|keine|kein fehler|keiner|0|false|off|aus|"
    r"betrieb|in betrieb|running|idle|bereit|ready|standby)$",
    re.IGNORECASE,
)
_ERROR = re.compile(r"(error|fehler|fault|störung|stoerung|alarm|defekt|failure)", re.IGNORECASE)
_QUIET = re.compile(
    r"^(kein|keine|none|no|ok|0+|ff|255|a0x0000|leer|nicht)$|^(kein|keine|none|no|ok|leer|nicht)\b"
    r"|^no_(alarm|notification|warning)s?$|kein alarm|keine benachrichtigung|keine warnung|"
    r"no alarm|^[-–—]+$",
    re.IGNORECASE,
)
_VALVE = re.compile(r"(haupt|main|absperr|shutoff|leck|leak|wasser_?ventil|water_?valve)")
_NOT_VALVE = re.compile(r"(garten|garden|beet|rasen)")
INVALID = ("unknown", "unavailable", "")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Technik:
    """Check the house technology and tell about problems."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        notify: Callable[..., None],
    ) -> None:
        """Initialize (notify(src, title, message, tag, urgent=, actions=))."""
        self.hass = hass
        self.store = store
        self.notify = notify
        self._unsub: list[CALLBACK_TYPE] = []
        self._listeners: list[CALLBACK_TYPE] = []
        self._sent: set[str] = set()
        self._primed = False
        self._flow_since: int | None = None
        self._starts: list[int] = []
        self.hints: dict[str, dict[str, Any]] = {}
        self.on_settings: Callable[[], None] | None = None
        self.overlay: Callable[[], dict[str, Any]] | None = None

    # ---------------------------------------------------------------- settings

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored settings and samples."""
        return self.store.data["technik"]

    def settings(self) -> dict[str, Any]:
        """Return the settings with defaults."""
        out = {**DEFAULTS, **{k: v for k, v in self.data.items() if k in DEFAULTS}}
        out["checks"] = {**DEFAULTS["checks"], **(self.data.get("checks") or {})}
        # devices linked in Core (catalog) win over what the dashboard sent
        for key, value in (self.overlay() if self.overlay else {}).items():
            if key in ("battery", "reach", "syr"):
                out[key] = {**(out.get(key) or {}), **value}
            else:
                out[key] = value
        return out

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Take the sensors from the dashboard (thresholds the user set as entities stay)."""
        own = self.data.get("own") or {}
        for key in DEFAULTS:
            if key == "checks" or key not in settings or key in own:
                continue
            self.data[key] = settings[key]
        self.data["configured"] = True
        if self.on_settings is not None:
            self.on_settings()
        self._listen()
        self.evaluate()
        self.store.save(SIGNAL_TECHNIK, SIGNAL_DATA)

    @callback
    def set_option(self, key: str, value: Any) -> None:
        """Change a threshold or a check (from the entities; the dashboard does not overwrite)."""
        if key in DEFAULTS["checks"]:
            self.data["checks"] = {**self.settings()["checks"], key: bool(value)}
        else:
            self.data[key] = value
            self.data["own"] = {**(self.data.get("own") or {}), key: True}
        self.evaluate()
        self.store.save(SIGNAL_TECHNIK, SIGNAL_DATA)

    # ---------------------------------------------------------------- listening

    @callback
    def async_setup(self) -> None:
        """Start the checks."""
        self._listen()
        self._unsub.append(async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))
        self.evaluate()

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in [*self._unsub, *self._listeners]:
            unsub()
        self._unsub.clear()
        self._listeners = []

    @callback
    def _listen(self) -> None:
        for unsub in self._listeners:
            unsub()
        self._listeners = []
        s = self.settings()
        flow, comp = s["flow"], s["compressor"]
        watched = {flow, comp} - {None}
        if watched:

            @callback
            def relevant(data: EventStateChangedData) -> bool:
                return data["entity_id"] in watched

            @callback
            def changed(event: Event[EventStateChangedData]) -> None:
                eid = event.data["entity_id"]
                old, new = event.data["old_state"], event.data["new_state"]
                if new is None:
                    return
                if eid == comp and new.state == "on" and old is not None and old.state != "on":
                    self._starts.append(now_ms())
                if eid == flow:
                    self._flow_changed(new)

            self._listeners.append(
                self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=relevant)
            )
        if flow and (state := self.hass.states.get(flow)) is not None:
            self._flow_changed(state)

        @callback
        def action(event: Event) -> None:
            if event.data.get("action") == ACTION_VALVE and (valve := self.valve()):
                self.hass.async_create_task(self._close_valve(valve))

        self._listeners.append(self.hass.bus.async_listen("mobile_app_notification_action", action))

    @callback
    def _flow_changed(self, state: State) -> None:
        value = _num(state.state)
        if value is None or value <= FLOW_ON:
            self._flow_since = None
        elif self._flow_since is None:
            self._flow_since = now_ms()

    def valve(self) -> str | None:
        """Return the main water valve (chosen, else found by its name)."""
        own = self.settings()["valve"]
        if own and self.hass.states.get(own) is not None:
            return own
        for state in [*self.hass.states.async_all("valve"), *self.hass.states.async_all("switch")]:
            text = f"{state.entity_id} {state.attributes.get('friendly_name', '')}".lower()
            if _VALVE.search(text) and not _NOT_VALVE.search(state.entity_id):
                return state.entity_id
        return None

    async def _close_valve(self, valve: str) -> None:
        domain = valve.split(".")[0]
        service = "close_valve" if domain == "valve" else "turn_off"
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": valve}, blocking=True
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("Could not close %s: %s", valve, err)

    # ---------------------------------------------------------------- checks

    def _state(self, entity_id: str | None) -> State | None:
        state = self.hass.states.get(entity_id or "")
        return state if state is not None and state.state not in INVALID else None

    def _fault(self, entity_id: str | None, ok: list[str] | None = None) -> str | None:
        state = self._state(entity_id)
        if state is None or _BENIGN.search(state.state):
            return None
        if state.domain == "binary_sensor":
            return "Störung" if state.state == "on" else None
        if ok is not None:
            return None if state.state in [str(x) for x in ok] else state.state
        if _NO_ERROR.match(state.state.strip()) or not _ERROR.search(state.state):
            return None
        return state.state

    @callback
    def _tick(self, _now: Any = None) -> None:
        self._sample()
        self.evaluate()

    @callback
    def _sample(self) -> None:
        """Daily minimum of the pressure, nightly minimum of the house consumption."""
        local = dt_util.now()
        day = local.date().isoformat()
        s = self.settings()
        changed = False
        for key, value, keep in (
            ("pressure_days", _num(getattr(self._state(s["pressure"]), "state", None)), DAYS_KEEP),
            ("base_nights", self.house_power() if local.hour < 5 else None, NIGHTS_KEEP),
        ):
            if value is None or value <= 0:
                continue
            samples = self.data.setdefault(key, {})
            if day in samples and samples[day] <= value:
                continue
            samples[day] = value
            for old in sorted(samples)[:-keep]:
                samples.pop(old)
            changed = True
        if changed:  # only on a new minimum (SD cards)
            self.store.save()

    def _watts(self, entity_id: str | None) -> float | None:
        state = self._state(entity_id)
        value = _num(state.state) if state else None
        if value is None:
            return None
        return value * 1000 if state.attributes.get("unit_of_measurement") == "kW" else value

    def house_power(self) -> float | None:
        """Return what the house uses now (W)."""
        p = self.settings()["power"] or {}
        if p.get("house"):
            value = self._watts(p["house"])
            return abs(value) if value is not None else None
        pv, grid, bat = (self._watts(p.get(k)) for k in ("pv", "grid", "battery"))
        if pv is None or grid is None:
            return None
        house = pv + (-1 if p.get("grid_invert") else 1) * grid
        house -= (-1 if p.get("battery_invert") else 1) * (bat or 0)
        return house if house >= 0 else None

    def pressure_trend(self) -> float | None:
        """Return the change of the daily minimum pressure per week (bar), None if too few days."""
        days = sorted((self.data.get("pressure_days") or {}).items())
        if len(days) < 7:
            return None
        start = dt_util.parse_date(days[0][0])
        xs = [(dt_util.parse_date(d) - start).days for d, _v in days]
        ys = [v for _d, v in days]
        mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
        den = sum((x - mx) ** 2 for x in xs)
        return (
            None
            if not den
            else sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / den * 7
        )

    def _hints(self) -> dict[str, dict[str, Any]]:
        s = self.settings()
        checks = s["checks"]
        out: dict[str, dict[str, Any]] = {}

        def hint(key: str, src: str, title: str, message: str, **extra: Any) -> None:
            out[key] = {"src": src, "title": title, "message": message, **extra}

        if err := self._fault(s["heatpump_error"]):
            hint("hp_error", "ht.heating", "Wärmepumpe", f"Wärmepumpe: {err}", urgent=True)
        if err := self._fault(s["inverter_error"], s["inverter_error_ok"]):
            hint("inv_error", "ht.energy", "Wechselrichter", f"Wechselrichter: {err}")
        now = dt_util.utcnow()
        for label, entity in (s["reach"] or {}).items():
            state = self.hass.states.get(entity or "")
            if (
                state is not None
                and state.state == "unavailable"
                and now - state.last_changed >= timedelta(minutes=UNREACHABLE_MIN)
            ):
                src = "ht.heating" if label == "Heizkreis" else "ht.energy"
                hint(f"reach|{label}", src, label, f"{label}: nicht erreichbar")
        pressure = _num(getattr(self._state(s["pressure"]), "state", None))
        if checks["pressure"] and pressure is not None and 0 < pressure < float(s["pressure_min"]):
            crit = pressure < float(s["pressure_critical"])
            hint(
                "pressure",
                "ht.heating",
                "Heizung",
                f"Heizungswasser nachfüllen ({pressure:.2f} bar)".replace(".", ","),
                urgent=crit,
            )
        trend = self.pressure_trend() if checks["pressure"] else None
        if trend is not None and trend <= -float(s["pressure_drop"]):
            week = dt_util.now().isocalendar()
            hint(
                f"pdrop|{week[0]}-{week[1]}",
                "ht.heating",
                "Heizung",
                f"Heizungsdruck fällt ({trend:.2f} bar pro Woche)".replace(".", ","),
            )
        salt = _num(getattr(self._state(s["salt"]), "state", None))
        if salt is not None and salt <= float(s["salt_max"]) * 0.2:
            hint("salt", "ht.water", "Wasser", f"Salz nachfüllen ({round(salt)} kg)")
        for kind, entity in (s["syr"] or {}).items():
            state = self._state(entity)
            if state is not None and not _QUIET.search(state.state.strip()):
                hint(
                    f"syr|{kind}|{state.state}",
                    "ht.water",
                    "Entkalker",
                    f"Entkalker-{kind}: {state.state}",
                )
        if checks["water"] and self._flow_since:
            mins = (now_ms() - self._flow_since) / 60000
            local = dt_util.now()
            night = NIGHT[0] <= local.hour <= NIGHT[1]
            if mins >= float(s["water_minutes"]) or (night and mins >= 10):
                valve = self.valve()
                hint(
                    f"water|{self._flow_since}",
                    "ht.water",
                    "Wasser",
                    f"Seit {round(mins)} Minuten fließt Wasser"
                    + (" – mitten in der Nacht" if night else "")
                    + ". Läuft ein Gerät, der Garten oder ein Hahn? Sonst nach einem Leck schauen.",
                    urgent=night,
                    actions=[
                        {"action": ACTION_VALVE, "title": "Wasser absperren", "destructive": True}
                    ]
                    if valve
                    else None,
                )
        cutoff = now_ms() - 6 * 3_600_000
        self._starts = [t for t in self._starts if t >= cutoff]
        per_hour = len(self._starts) / 6
        if checks["cycling"] and s["compressor"] and per_hour > float(s["cycles_per_hour"]):
            hint(
                f"cycling|{dt_util.now().date().isoformat()}",
                "ht.heating",
                "Wärmepumpe",
                f"Wärmepumpe taktet ({per_hour:.1f} Starts pro Stunde)".replace(".", ","),
            )
        if checks["baseload"] and dt_util.now().hour >= 5:
            nights = self.data.get("base_nights") or {}
            today = dt_util.now().date().isoformat()
            others = [v for d, v in nights.items() if d != today]
            last = nights.get(today)
            if last is not None and len(others) >= 5:
                typical = median(others)
                if last > typical * 1.5 + 60:
                    hint(
                        f"base|{today}",
                        "ht.energy",
                        "Grundlast",
                        f"Grundlast letzte Nacht {round(last)} W statt üblich {round(typical)} W",
                    )
        return out

    @callback
    def evaluate(self) -> None:
        """Check everything, send what is new."""
        hints = self._hints()
        if self._primed:
            for key, h in hints.items():
                if key not in self._sent:
                    self.notify(
                        h["src"],
                        h["title"],
                        h["message"],
                        f"technik|{key.split('|')[0]}",
                        urgent=bool(h.get("urgent")),
                        actions=h.get("actions"),
                    )
        self._sent = set(hints)
        self._primed = True
        if hints != self.hints:
            self.hints = hints
            self.store.save(SIGNAL_TECHNIK, SIGNAL_DATA)

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        s = self.settings()
        return {
            "configured": bool(self.data.get("configured")),
            "hints": [
                {
                    "key": k,
                    "src": h["src"],
                    "message": h["message"],
                    "urgent": bool(h.get("urgent")),
                }
                for k, h in self.hints.items()
            ],
            "checks": s["checks"],
            "pressure_min": s["pressure_min"],
            "water_minutes": s["water_minutes"],
            "pressure_trend": self.pressure_trend(),
            "valve": self.valve(),
        }


class TechnikEntity(CoreEntity):
    """An entity of the device "Haustechnik"."""

    def __init__(self, entry_id: str, technik: Technik, key: str, domain: str) -> None:
        """Initialize (<domain>.technik_<key>)."""
        super().__init__(entry_id, f"technik_{key}", DEVICE_TECHNIK, domain, key)
        self._technik = technik
        self._attr_translation_key = f"technik_{key}"

    async def async_added_to_hass(self) -> None:
        """Update with the checks."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_TECHNIK, self.async_write_ha_state)
        )


class TechnikOption(TechnikEntity):
    """A setting of the house technology checks."""

    _attr_entity_category = EntityCategory.CONFIG
