"""Household appliances: dishwasher, washer, dryer, fridge (and others) linked by their device.

An appliance is added as subentry ("Add household appliance") by picking its device. The rest is
found on the device the way the dashboard does it (Home Connect, Miele, LG ThinQ, Electrolux …):

* the operation state sensor (``running`` / ``finished`` / ``error`` / ``delayed start`` …),
  remaining time, progress, program, start time, door and energy sensor,
* for a fridge: temperature sensors (fridge / freezer), doors and an alarm.

What comes of it:

* runs (start, end, minutes, program, kWh) and the usual duration per program,
* "to unload" after a run (dishwasher, washer, dryer) until "unloaded" is pressed, the task is
  checked off, the door was opened or a new run starts – the dashboard's done record
  ``appl:device:<device id>`` is the same,
* notifications: finished, error, reminder to unload, solar power while waiting for the start;
  fridge door open, too warm, alarm.

Per appliance: ``{"run": {start, program, kwh}, "last": {start, end, minutes, program, kwh},
"unload": bool, "finished": ms}`` in ``appliances``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
import re
from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    State,
    callback,
)
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import slugify

from . import durations
from .const import SIGNAL_APPLIANCE, SIGNAL_DATA, SIGNAL_TASKS, TICK_INTERVAL
from .mower import CONF_DEVICE
from .store import CoreStore, now_ms

SUBENTRY_APPLIANCE = "appliance"
CONF_KIND = "kind"
KIND_OPTIONS = ["auto", "dishwasher", "washer", "dryer", "fridge", "other"]
UNLOAD_KINDS = ("dishwasher", "washer", "dryer")
SOURCES = {
    "dishwasher": "hh.dish",
    "washer": "hh.laundry",
    "dryer": "hh.laundry",
    "fridge": "hh.fridge",
}
LABELS = {
    "dishwasher": "Geschirrspüler",
    "washer": "Waschmaschine",
    "dryer": "Trockner",
    "fridge": "Kühlschrank",
    "other": "Haushaltsgerät",
}
ICONS = {
    "dishwasher": "mdi:dishwasher",
    "washer": "mdi:washing-machine",
    "dryer": "mdi:tumble-dryer",
    "fridge": "mdi:fridge-outline",
    "other": "mdi:devices",
}
DEFAULTS: dict[str, Any] = {
    "reminder": 30,  # minutes after the end; 0 = no reminder
    "solar_entity": None,
    "solar_min": 1500,
    "door_minutes": 3,
    "max_temp": 8,
    "freezer_max_temp": -15,
}
STATES = ["off", "idle", "waiting", "running", "finished", "error", "ok", "door_open", "warm"]
STATES += ["alarm"]
RUN_MIN_MINUTES = 5  # shorter "runs" (rinse, test, wrong program) are not counted
WARM_SECONDS = 900  # a fridge has to be too warm this long before it is reported
INVALID = ("unknown", "unavailable", "")

_KINDS = [
    (r"(geschirr|spülmaschine|spuelmaschine|spüler|spueler|dishwasher)", "dishwasher"),
    (r"(kühl|kuehl|fridge|refrigerator|gefrier|freezer)", "fridge"),
    (r"(waschmaschine|washer|washing machine|waschtrockner)", "washer"),
    (r"(trockner|dryer)", "dryer"),
]
_STATUS = re.compile(
    r"(operation_state|betriebszustand|betriebsstatus|programmstatus|program_phase|"
    r"programmphase|status|zustand|state)"
)
_STATUS_NOT = re.compile(r"(door|tür|tuer|remote|fern|connect|verbind|wifi|signal)")
_REMAINING = re.compile(
    r"(remaining|restlaufzeit|restzeit|verbleib|finish|fertig um|end_time|endzeit|program_end|"
    r"programmende)"
)
_PROGRESS = re.compile(r"(progress|fortschritt)")
_PROGRAM = re.compile(r"(program|programm|course)")
_PROGRAM_NOT = re.compile(
    r"(progress|fortschritt|remaining|rest|ende|end|phase|status|state|start)"
)
_START = re.compile(
    r"(start_time|startzeit|start_in|startet|relative_hour_to_start|to_start|delayed_start|"
    r"startvorwahl|start_delay)"
)
_START_NOT = re.compile(r"(elapsed|verstrichen)")
_DOOR = re.compile(r"(door|tür|tuer)")
_DOOR_BAD = re.compile(
    r"(lock|verriegel|sperr|kindersich|child|alarm|warn|indoor|remote|fern|connect|signal|assist|"
    r"auto.?open|auto.?dry|autodry|öffnungs|oeffnungs|opener|türöffn|tueroeffn|open.?door|"
    r"door.?open)"
)
_DOOR_STATE = re.compile(
    r"^(open|opened|offen|geöffnet|geoeffnet|ajar|closed|close|geschlossen|zu|locked|verriegelt)$"
)
_OPEN = re.compile(r"^(on|open|opened|offen|geöffnet|geoeffnet|ajar)$")
_WAITING = re.compile(
    r"(waiting_to_start|programmed|reservation|reserved|delayed|startvorwahl|start_delay|wartet)"
)
_NOT_RUN = re.compile(
    r"(inaktiv|inactive|not_running|not running|nicht aktiv|deaktiv|deactivated|run_state)"
)
_IDLE = re.compile(
    r"^(off|aus|ausgeschaltet|standby|bereit|ready|idle|inactive|inaktiv|power_off|not_connected|"
    r"initial|unavailable|unknown|none|leerlauf|eco_standby)$"
)
_RUN = re.compile(
    r"(run|läuft|laeuft|in_betrieb|in_use|active|aktiv|cleaning|washing|drying|rinsing|rinse|"
    r"spinning|schleuder|waschen|wäscht|trocknen|trocknet|detecting|spülen|heating|cooking|"
    r"brewing|on_run)"
)
_NOT_RUN_LEFT = re.compile(r"(finish|ready|off|idle|end|fertig|beendet|bereit|aus$|standby|pause)")
_FINISHED = re.compile(r"(finish|fertig|done|complete|beendet|ended|end$)")
_ERROR = re.compile(r"(error|fehler|fault|failure|störung|action_?required)")
_OFF = ("off", "unavailable", "unknown", "standby", "inactive", "idle", "ready", "power_off")
_OFF += ("not_connected", "initial", "on", "")
_FREEZE = re.compile(r"(freez|gefrier|tiefkühl|tiefkuehl|frost)")
_ALARM = re.compile(r"(alarm|warn)")
_ALARM_ON = re.compile(r"^(on|present|alarm|triggered|true)$")


def detect_kind(text: str) -> str:
    """Return dishwasher / washer / dryer / fridge / other from a device name and model."""
    low = text.lower()
    return next((kind for pattern, kind in _KINDS if re.search(pattern, low)), "other")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _watts(state: State | None) -> float | None:
    value = _num(state.state) if state else None
    if value is None or state is None:
        return None
    unit = state.attributes.get("unit_of_measurement", "W")
    return value * 1000 if unit == "kW" else value


class ApplianceTracker:
    """One configured appliance."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, subentry: ConfigSubentry) -> None:
        """Initialize the tracker."""
        self.hass = hass
        self.store = store
        self.id = subentry.subentry_id
        self.name = subentry.title
        self.device_id: str = subentry.data[CONF_DEVICE]
        self._kind = subentry.data.get(CONF_KIND) or "auto"
        self.options = {
            **DEFAULTS,
            **{k: v for k, v in subentry.data.items() if k in DEFAULTS and v is not None},
        }
        self.status: dict[str, Any] = {"state": "off"}
        self.on_notify: Callable[[ApplianceTracker, str, str], None] | None = None
        self._primed = False
        self._sent: set[str] = set()
        self._warm_since: dict[str, int] = {}

    # ---------------------------------------------------------------- device and entities

    @property
    def kind(self) -> str:
        """Return the kind (chosen or found from the device name and model)."""
        if self._kind != "auto":
            return self._kind
        device = dr.async_get(self.hass).async_get(self.device_id)
        if device is None:
            return "other"
        return detect_kind(f"{device.name_by_user or ''} {device.name or ''} {device.model or ''}")

    @property
    def source(self) -> str:
        """Return the notification source (dashboard settings)."""
        return SOURCES.get(self.kind, "hh.appliances")

    @property
    def label(self) -> str:
        """Return the kind as text (notification title)."""
        return LABELS.get(self.kind, LABELS["other"])

    @property
    def unloads(self) -> bool:
        """Return True for appliances that are emptied after a run."""
        return self.kind in UNLOAD_KINDS

    @property
    def ack_key(self) -> str:
        """Return the done key of "unloaded" (shared with the dashboard)."""
        return f"appl:device:{self.device_id}"

    def device_identifiers(self) -> set[tuple[str, str]]:
        """Return the identifiers of the device (our entities are added to it)."""
        device = dr.async_get(self.hass).async_get(self.device_id)
        return set(device.identifiers) if device else set()

    def entities(self) -> list[er.RegistryEntry]:
        """Return the entities of the device (not our own)."""
        return [
            e
            for e in er.async_entries_for_device(er.async_get(self.hass), self.device_id)
            if e.platform != "smarthome_ui"
        ]

    def _text(self, entry: er.RegistryEntry) -> str:
        state = self.hass.states.get(entry.entity_id)
        name = state.attributes.get("friendly_name", "") if state else ""
        return f"{entry.translation_key or ''} {entry.entity_id} {name}".lower()

    def _find(
        self,
        domains: tuple[str, ...],
        pattern: re.Pattern[str],
        not_: re.Pattern[str] | None = None,
    ) -> str | None:
        for entry in self.entities():
            if entry.domain not in domains:
                continue
            text = self._text(entry)
            if pattern.search(text) and not (not_ and not_.search(text)):
                return entry.entity_id
        return None

    def _state(self, entity_id: str | None) -> State | None:
        return self.hass.states.get(entity_id) if entity_id else None

    def _valid(self, entity_id: str | None) -> State | None:
        state = self._state(entity_id)
        return state if state is not None and state.state not in INVALID else None

    @property
    def state_entity(self) -> str | None:
        """Return the operation state sensor (else the power switch)."""
        return self._find(("sensor",), _STATUS, _STATUS_NOT) or next(
            (e.entity_id for e in self.entities() if e.domain == "switch"), None
        )

    @property
    def door_entity(self) -> str | None:
        """Return the door: a door state sensor or a door binary sensor (not lock, alarm …)."""
        entries = self.entities()

        def ok(entry: er.RegistryEntry) -> bool:
            return (
                not _DOOR_BAD.search(self._text(entry)) and self._valid(entry.entity_id) is not None
            )

        for entry in entries:
            state = self._state(entry.entity_id)
            if (
                entry.domain == "sensor"
                and _DOOR.search(self._text(entry))
                and ok(entry)
                and state is not None
                and _DOOR_STATE.match(state.state.lower())
            ):
                return entry.entity_id
        for entry in entries:
            state = self._state(entry.entity_id)
            if (
                entry.domain == "binary_sensor"
                and state is not None
                and (
                    state.attributes.get("device_class") in ("door", "opening")
                    or (
                        not state.attributes.get("device_class")
                        and re.search(r"(^|[\s_.])(door|tür|tuer)([\s_.]|$)", self._text(entry))
                    )
                )
                and ok(entry)
            ):
                return entry.entity_id
        return None

    def _open(self, entity_id: str | None) -> bool:
        state = self._valid(entity_id)
        return state is not None and bool(_OPEN.match(state.state.lower()))

    def _energy(self) -> float | None:
        for entry in self.entities():
            state = self._valid(entry.entity_id)
            if (
                entry.domain == "sensor"
                and state is not None
                and state.attributes.get("device_class") == "energy"
            ):
                value = _num(state.state)
                if value is not None and state.attributes.get("unit_of_measurement") == "Wh":
                    value /= 1000
                return value
        return None

    def _remaining(self) -> float | None:
        state = self._valid(self._find(("sensor",), _REMAINING))
        if state is None:
            return None
        if state.attributes.get("device_class") == "timestamp":
            try:
                end = datetime.fromisoformat(state.state)
            except ValueError:
                return None
            return max(0, round((_ms(end) - now_ms()) / 60000))
        value = _num(state.state)
        if value is None:
            return None
        unit = state.attributes.get("unit_of_measurement")
        return value * 60 if unit == "h" else value / 60 if unit == "s" else value

    def _start_at(self) -> int | None:
        state = self._valid(self._find(("sensor",), _START, _START_NOT))
        if state is None:
            return None
        if state.attributes.get("device_class") == "timestamp":
            try:
                return _ms(datetime.fromisoformat(state.state))
            except ValueError:
                return None
        value = _num(state.state)
        if not value or value <= 0:
            return None
        unit = state.attributes.get("unit_of_measurement")
        return now_ms() + int(value * {"h": 3600, "s": 1}.get(unit, 60) * 1000)

    def _program(self) -> str | None:
        program = self._find(("select",), _PROGRAM) or self._find(
            ("sensor",), _PROGRAM, _PROGRAM_NOT
        )
        state = self._valid(program)
        return state.state if state is not None and state.state.lower() != "none" else None

    # ---------------------------------------------------------------- state

    def _data(self) -> dict[str, Any]:
        return self.store.data["appliances"].setdefault(self.id, {})

    def to_unload(self) -> bool:
        """Return True while the appliance waits to be emptied."""
        return bool(self._data().get("unload"))

    def _machine(self, now: int) -> tuple[dict[str, Any], dict[str, str], bool]:
        """Washer, dryer, dishwasher …: state, runs, unloading."""
        status_state = self._state(self.state_entity)
        raw = (status_state.state if status_state else "").lower()
        minutes = self._remaining()
        progress_state = self._valid(self._find(("sensor",), _PROGRESS))
        progress = (
            _num(progress_state.state)
            if progress_state is not None
            and progress_state.attributes.get("unit_of_measurement") == "%"
            else None
        )
        waiting = bool(_WAITING.search(raw))
        finished = bool(_FINISHED.search(raw))
        running = (
            not waiting
            and not finished
            and not _IDLE.match(raw)
            and bool(
                _RUN.search(_NOT_RUN.sub("", raw))
                or (progress is not None and 0 < progress < 100)
                or (minutes is not None and minutes > 0 and not _NOT_RUN_LEFT.search(raw))
            )
        )
        error = bool(_ERROR.search(raw))
        program = self._program()
        state = (
            "error"
            if error
            else "running"
            if running
            else "waiting"
            if waiting
            else "finished"
            if finished
            else "off"
            if raw in _OFF
            else "idle"
        )
        data = self._data()
        changed = False
        notes: dict[str, str] = {}
        run = data.get("run")
        if state == "running" and not run:
            data["run"] = {"start": now, "program": program, "kwh": self._energy()}
            data["unload"] = False
            changed = True
        elif run and state == "running" and program and run.get("program") != program:
            run["program"] = program
            changed = True
        elif run and state not in ("running", "error"):
            changed = True
            data.pop("run")
            mins = round((now - run["start"]) / 60000)
            if mins >= RUN_MIN_MINUTES:
                energy, start_kwh = self._energy(), run.get("kwh")
                kwh = (
                    round(energy - start_kwh, 2)
                    if energy is not None and start_kwh is not None and energy >= start_kwh
                    else None
                )
                prog = run.get("program") or program
                data["last"] = {
                    "start": run["start"],
                    "end": now,
                    "minutes": mins,
                    "program": prog,
                    "kwh": kwh,
                }
                if self.unloads:
                    data["unload"], data["finished"] = True, now
                self._send("done", f"{self.name} fertig")
                result = durations.record(self.store, self.id, prog or "run", mins, min_diff=10)
                if result is not None:
                    what = f" ({prog})" if prog else ""
                    mins_text = f"{result['minutes']} min statt üblich {result['usual']} min"
                    self._send("duration", f"{self.name}{what}: {mins_text}")
        elif (
            finished
            and not run
            and self.unloads
            and status_state is not None
            and not data.get("unload")
        ):
            # finished while Home Assistant (or the integration) was not running
            end = _ms(status_state.last_changed)
            ack = int((self.store.done.get(self.ack_key) or {}).get("t") or 0)
            if end > int(data.get("finished") or 0) + 60_000 and ack < end:
                data["unload"], data["finished"] = True, end
                changed = True
        if data.get("unload"):
            finished_at = int(data.get("finished") or 0)
            ack = int((self.store.done.get(self.ack_key) or {}).get("t") or 0)
            door = self._state(self.door_entity)
            door_at = _ms(door.last_changed) if door is not None else 0
            if ack >= finished_at or door_at > finished_at + 30_000:
                data["unload"] = False
                changed = True
            elif (reminder := int(self.options["reminder"] or 0)) and now - finished_at >= (
                reminder * 60_000
            ):
                notes[f"remind|{finished_at}"] = (
                    f"{self.name} ist seit {round((now - finished_at) / 60000)} min fertig – "
                    "bitte ausräumen"
                )
        if error:
            notes["error"] = f"{self.name}: Störung"
        if waiting and (watts := _watts(self._state(self.options["solar_entity"]))) is not None:
            if watts >= float(self.options["solar_min"] or 0):
                notes["solar"] = f"Sonnenstrom übrig – {self.name} jetzt starten?"
        status = {
            "state": state,
            "minutes": round(minutes) if minutes is not None and state == "running" else None,
            "progress": progress if state == "running" else None,
            "program": program,
            "start_at": self._start_at() if waiting else None,
            "door": self._open(self.door_entity),
        }
        return status, notes, changed

    def _temps(self) -> tuple[float | None, float | None]:
        fridge = freezer = None
        for entry in self.entities():
            state = self._valid(entry.entity_id)
            if (
                entry.domain != "sensor"
                or state is None
                or state.attributes.get("device_class") != "temperature"
            ):
                continue
            value = _num(state.state)
            if _FREEZE.search(self._text(entry)):
                freezer = value if freezer is None else freezer
            elif fridge is None:
                fridge = value
        return fridge, freezer

    def _fridge(self, now: int) -> tuple[dict[str, Any], dict[str, str], bool]:
        """Fridge: temperatures, doors, alarm."""
        fridge_t, freezer_t = self._temps()
        doors = [
            e.entity_id
            for e in self.entities()
            if e.domain in ("binary_sensor", "sensor")
            and _DOOR.search(self._text(e))
            and not re.search(r"(alarm|lock)", self._text(e))
        ]
        open_since = [
            _ms(state.last_changed)
            for d in doors
            if self._open(d) and (state := self._state(d)) is not None
        ]
        alarm = self._find(("binary_sensor", "event", "sensor"), _ALARM)
        alarm_on = (state := self._valid(alarm)) is not None and bool(
            _ALARM_ON.match(state.state.lower())
        )
        notes: dict[str, str] = {}
        warm = {
            "warm": fridge_t is not None and fridge_t >= float(self.options["max_temp"]),
            "freezer": freezer_t is not None
            and freezer_t > float(self.options["freezer_max_temp"]),
        }
        for key, value in warm.items():
            if not value:
                self._warm_since.pop(key, None)
            elif now - self._warm_since.setdefault(key, now) >= WARM_SECONDS * 1000:
                part, temp = ("Kühlteil", fridge_t) if key == "warm" else ("Gefrierteil", freezer_t)
                notes[key] = f"{self.name}: {part} zu warm ({round(temp or 0)} °C)"
        if open_since and now - min(open_since) >= float(self.options["door_minutes"]) * 60_000:
            mins = round((now - min(open_since)) / 60000)
            notes["door"] = f"{self.name}: Tür seit {mins} min offen"
        if alarm_on:
            notes["alarm"] = f"{self.name}: Alarm"
        if fridge_t is None and freezer_t is None and not doors:
            state = "off"
        else:
            state = (
                "alarm"
                if alarm_on
                else "warm"
                if any(warm.values())
                else "door_open"
                if open_since
                else "ok"
            )
        status = {
            "state": state,
            "temp": fridge_t,
            "freezer_temp": freezer_t,
            "door": bool(open_since),
        }
        return status, notes, False

    @callback
    def _send(self, key: str, message: str) -> None:
        if self._primed and self.on_notify is not None:
            self.on_notify(self, key, message)

    @callback
    def evaluate(self) -> None:
        """Read the device, follow runs and unloading, send what is new."""
        now = now_ms()
        before = self.to_unload()
        status, notes, changed = self._fridge(now) if self.kind == "fridge" else self._machine(now)
        for key, message in notes.items():
            if key not in self._sent:
                self._send(key, message)
        self._sent = set(notes)
        self._primed = True
        if changed:
            self.store.save(SIGNAL_DATA, *([SIGNAL_TASKS] if before != self.to_unload() else []))
        if changed or status != self.status:
            self.status = status
            async_dispatcher_send(self.hass, SIGNAL_APPLIANCE, self.id)

    @callback
    def unloaded(self, who: str | None = None) -> None:
        """Mark as emptied now."""
        self.store.mark_done(self.ack_key, who=who, label=f"{self.name} ausgeräumt")

    # ---------------------------------------------------------------- dashboard, tasks

    def last_run(self) -> dict[str, Any]:
        """Return the last run (start, end, minutes, program, kWh)."""
        return dict(self._data().get("last") or {})

    def usual(self, program: str | None = None) -> int | None:
        """Return the usual minutes of a program (default: the one of the last run)."""
        program = program or self.last_run().get("program")
        return durations.usual(self.store, self.id, program or "run")

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        data = self._data()
        return {
            "id": self.id,
            "name": self.name,
            "device_id": self.device_id,
            "kind": self.kind,
            "status": dict(self.status),
            "to_unload": self.to_unload(),
            "finished": data.get("finished"),
            "last": self.last_run() or None,
            "usual_minutes": self.usual(),
            "options": dict(self.options),
        }

    def tasks(self) -> list[dict[str, Any]]:
        """Return "<name> ausräumen" while it waits to be emptied."""
        if not self.to_unload():
            return []
        finished = int(self._data().get("finished") or 0)
        return [
            {
                "key": self.ack_key,
                "name": f"{self.name} ausräumen",
                "src": self.source,
                "area": None,
                "icon": ICONS.get(self.kind, ICONS["other"]),
                "interval": None,
                "last": finished,
                "due": finished,
            }
        ]


class ApplianceManager:
    """All configured appliances of the config entry."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self.trackers: dict[str, ApplianceTracker] = {}
        self.on_notify: Callable[[ApplianceTracker, str, str], None] | None = None
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_setup(self, subentries: list[ConfigSubentry]) -> None:
        """Start following the appliances."""
        for subentry in subentries:
            if subentry.subentry_type == SUBENTRY_APPLIANCE:
                tracker = ApplianceTracker(self.hass, self.store, subentry)
                tracker.on_notify = self.on_notify
                self.trackers[subentry.subentry_id] = tracker
        if not self.trackers:
            return
        registry = er.async_get(self.hass)
        devices = {t.device_id: t for t in self.trackers.values()}

        @callback
        def is_appliance_entity(data: EventStateChangedData) -> bool:
            entry = registry.async_get(data["entity_id"])
            return (
                entry is not None
                and entry.device_id in devices
                and entry.platform != "smarthome_ui"
            )

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            entry = registry.async_get(event.data["entity_id"])
            if entry is not None and (tracker := devices.get(entry.device_id or "")):
                tracker.evaluate()

        self._unsub.append(
            self.hass.bus.async_listen(
                EVENT_STATE_CHANGED, changed, event_filter=is_appliance_entity
            )
        )
        # "unloaded" checked off (dashboard, to-do list, button) and the clock (reminders)
        self._unsub.append(async_dispatcher_connect(self.hass, SIGNAL_DATA, self._tick))
        self._unsub.append(async_track_time_interval(self.hass, self._tick, TICK_INTERVAL))
        self._tick()

    @callback
    def _tick(self, _now: Any = None) -> None:
        for tracker in self.trackers.values():
            tracker.evaluate()

    @callback
    def async_stop(self) -> None:
        """Stop following."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def info(self) -> list[dict[str, Any]]:
        """Return the appliances for the dashboard."""
        return [t.info() for t in self.trackers.values()]

    def tasks(self) -> list[dict[str, Any]]:
        """Return the appliances waiting to be emptied as tasks."""
        return [task for t in self.trackers.values() for task in t.tasks()]


class ApplianceEntity(Entity):
    """An entity of an appliance; belongs to the appliance's own device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry_id: str, tracker: ApplianceTracker, key: str, domain: str) -> None:
        """Initialize (entity ID: <domain>.household_<appliance>_<key>)."""
        self._tracker = tracker
        self._attr_unique_id = f"{entry_id}_appliance_{tracker.id}_{key}"
        self._attr_translation_key = f"appliance_{key}"
        self.entity_id = f"{domain}.household_{slugify(tracker.name)}_{key}"
        if identifiers := tracker.device_identifiers():
            self._attr_device_info = DeviceInfo(identifiers=identifiers)

    async def async_added_to_hass(self) -> None:
        """Update when the appliance changes."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_APPLIANCE, self._changed))

    @callback
    def _changed(self, appliance_id: str) -> None:
        if appliance_id == self._tracker.id:
            self.async_write_ha_state()


@callback
def async_setup_appliance_entities(
    manager: ApplianceManager,
    async_add_entities: AddConfigEntryEntitiesCallback,
    create: Callable[[ApplianceTracker], list[Entity]],
) -> None:
    """Add the entities of every appliance."""
    for tracker in manager.trackers.values():
        if entities := create(tracker):
            async_add_entities(entities, config_subentry_id=tracker.id)
