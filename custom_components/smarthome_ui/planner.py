"""Robot planner: start the mower / vacuum on its own when areas are due.

Per robot a main switch "mow / vacuum automatically" (off by default) and one switch per area
or room. Every minute the planner checks, in this order, and waits while one is not met
(each condition can be switched off when changing the mower / vacuum):

* something is due (due areas whose switch is on),
* time window (e.g. 9:00–18:00),
* not in the quiet hours of the notifications,
* nobody at home (``person.*``),
* no rain (mower: weather and the mower's rain sensor),
* enough solar power (a power sensor with PV production or surplus, at least N watts),
* the robot is ready (in the station, no error).

Then it announces the start (push with "Not today", N minutes ahead) and starts with the due
areas: vacuum ``vacuum.clean_area`` (else ``vacuum.start``), mower: area switches on/off and
``lawn_mower.start_mowing`` (else a job button with the area's name). At most one automatic
start per robot and day.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time as dtime
import logging
from typing import Any

from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    PLANNER_INTERVAL,
    SIGNAL_DATA,
    SIGNAL_MOWER,
    SIGNAL_PLANNER,
    SIGNAL_VACUUM,
)
from .mower import MowerTracker, area_name
from .notify import NotifyEngine, in_quiet
from .store import CoreStore, now_ms
from .tasks import task_states
from .vacuum import VacuumTracker

_LOGGER = logging.getLogger(__name__)

KIND_MOWER = "mower"
KIND_VACUUM = "vacuum"
RAIN = ("rainy", "pouring", "lightning-rainy", "snowy-rainy", "hail", "snowy", "lightning")
DEFAULTS: dict[str, dict[str, Any]] = {
    KIND_MOWER: {
        "auto_from": "10:00",
        "auto_to": "19:00",
        "cond_quiet": True,
        "cond_away": False,
        "cond_rain": True,
        "cond_solar": False,
        "solar_entity": None,
        "solar_min": 1000,
        "announce": 10,
    },
    KIND_VACUUM: {
        "auto_from": "09:00",
        "auto_to": "18:00",
        "cond_quiet": True,
        "cond_away": True,
        "cond_rain": False,
        "cond_solar": False,
        "solar_entity": None,
        "solar_min": 1000,
        "announce": 10,
        "mop_every": 7,
    },
}
STATUS = [
    "off",
    "skipped",
    "done_today",
    "nothing_due",
    "waiting_window",
    "waiting_quiet",
    "waiting_home",
    "waiting_rain",
    "waiting_solar",
    "not_ready",
    "announced",
    "started",
    "mop_pending",
    "mopping",
]
MOP_WAIT_MS = 2 * 60_000  # after vacuuming: let the robot dock and empty before mopping
MOP_GIVE_UP_MS = 6 * 3600_000
ACTION_SKIP = "SMARTHOME_SKIP_"


def _hhmm(value: Any, default: dtime) -> dtime:
    try:
        hour, minute = str(value).split(":")[:2]
        return dtime(int(hour), int(minute))
    except (TypeError, ValueError):
        return default


def _watts(state: Any) -> float | None:
    try:
        value = float(state.state)
    except (AttributeError, TypeError, ValueError):
        return None
    unit = state.attributes.get("unit_of_measurement", "W")
    return value * 1000 if unit == "kW" else value * 1_000_000 if unit == "MW" else value


class RobotPlanner:
    """Plan one mower or vacuum."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        engine: NotifyEngine,
        tracker: MowerTracker | VacuumTracker,
        kind: str,
        subentry: ConfigSubentry,
    ) -> None:
        """Initialize the planner."""
        self.hass = hass
        self.store = store
        self.engine = engine
        self.tracker = tracker
        self.kind = kind
        self.id = tracker.id
        self.settings = {
            **DEFAULTS[kind],
            **{k: v for k, v in subentry.data.items() if v is not None},
        }
        self.status = "off"
        self.due: list[str] = []
        self.mop_due: list[str] = []
        self.start_at: int | None = None

    # ---------------------------------------------------------------- stored switches

    def _state(self) -> dict[str, Any]:
        return self.store.data["planner"].setdefault(
            self.id, {"on": False, "off": [], "skip": None, "started": None}
        )

    @property
    def is_on(self) -> bool:
        """Return True if the robot may start on its own."""
        return bool(self._state().get("on"))

    @callback
    def set_on(self, on: bool) -> None:
        """Switch the planner on or off."""
        self._state()["on"] = on
        self.start_at = None
        self._changed(save=True)
        self.tick()

    def area_on(self, area: str) -> bool:
        """Return True if an area / room may be done automatically."""
        return area not in (self._state().get("off") or [])

    @callback
    def set_area(self, area: str, on: bool) -> None:
        """Switch one area / room on or off."""
        off = set(self._state().get("off") or [])
        off.discard(area) if on else off.add(area)
        self._state()["off"] = sorted(off)
        self._changed(save=True)
        self.tick()

    def mop_on(self, area: str) -> bool:
        """Return True if a room may be mopped automatically (off e.g. for carpets)."""
        return area not in (self._state().get("mop_off") or [])

    @callback
    def set_mop(self, area: str, on: bool) -> None:
        """Switch mopping of one room on or off."""
        off = set(self._state().get("mop_off") or [])
        off.discard(area) if on else off.add(area)
        self._state()["mop_off"] = sorted(off)
        self._changed(save=True)
        self.tick()

    @property
    def mop_every(self) -> int:
        """Return the days between two moppings (0 = the planner does not mop)."""
        if self.kind != KIND_VACUUM:
            return 0
        return int(self.settings.get("mop_every") or 0)

    def can_mop(self) -> bool:
        """Return True if the vacuum can mop and the planner should plan it."""
        return (
            isinstance(self.tracker, VacuumTracker)
            and self.mop_every > 0
            and self.tracker.mop_control() is not None
        )

    def mop_due_areas(self) -> list[str]:
        """Return the rooms due for mopping (never mopped or mopped longer ago than set)."""
        if not self.can_mop() or not isinstance(self.tracker, VacuumTracker):
            return []
        limit = now_ms() - self.mop_every * 86_400_000
        return [
            a
            for a in self.tracker.known_rooms()
            if self.area_on(a) and self.mop_on(a) and (self.tracker.last_mopped(a) or 0) <= limit
        ]

    @callback
    def skip_today(self) -> None:
        """Do not start today."""
        self._state()["skip"] = dt_util.now().date().isoformat()
        self.start_at = None
        self._changed(save=True)
        self.tick()

    def areas(self) -> list[str]:
        """Return the areas / rooms of the robot."""
        if isinstance(self.tracker, VacuumTracker):
            return self.tracker.known_rooms()
        return list(self.tracker.areas)

    def area_name(self, area: str) -> str:
        """Return the name of an area / room."""
        if isinstance(self.tracker, VacuumTracker):
            return self.tracker.area_name(area)
        return area

    # ---------------------------------------------------------------- conditions

    def due_areas(self) -> list[str]:
        """Return the due areas / rooms whose switch is on."""
        now = now_ms()
        out = []
        if isinstance(self.tracker, VacuumTracker):
            tasks = {t.get("key"): t for t in self.store.data["tasks"]}
            for area in self.tracker.known_rooms():
                task = tasks.get(f"clean:{area}")
                if task is None or not self.area_on(area):
                    continue
                state = task_states([task], self.store.done)[0]
                if state.due is not None and state.due <= now:
                    out.append(area)
            return out
        for area in self.tracker.areas:
            due = self.tracker.due(area)
            if self.area_on(area) and due is not None and due <= now:
                out.append(area)
        return out

    def blocker(self, local: datetime) -> str | None:
        """Return why the robot may not start now (None = it may)."""
        s = self.settings
        start, end = _hhmm(s.get("auto_from"), dtime(9, 0)), _hhmm(s.get("auto_to"), dtime(18, 0))
        t = local.time()
        inside = (start <= t < end) if start <= end else (t >= start or t < end)
        if not inside:
            return "waiting_window"
        if s.get("cond_quiet") and in_quiet(self.store.data["notify"].get("quiet"), local):
            return "waiting_quiet"
        if s.get("cond_away") and any(
            st.state == "home" for st in self.hass.states.async_all("person")
        ):
            return "waiting_home"
        if s.get("cond_rain") and self._raining():
            return "waiting_rain"
        if s.get("cond_solar"):
            watts = _watts(self.hass.states.get(s.get("solar_entity") or ""))
            if watts is None or watts < float(s.get("solar_min") or 0):
                return "waiting_solar"
        if not self._ready():
            return "not_ready"
        return None

    def _raining(self) -> bool:
        weather = next(iter(self.hass.states.async_all("weather")), None)
        if weather is not None and weather.state in RAIN:
            return True
        for entry in self.tracker.entities():
            if entry.domain == "binary_sensor" and (
                entry.translation_key == "rain_detected" or "rain" in entry.entity_id
            ):
                state = self.hass.states.get(entry.entity_id)
                if state is not None and state.state == "on":
                    return True
        return False

    def _ready(self) -> bool:
        state = self.hass.states.get(self.tracker.entity_id or "")
        if state is None:
            return False
        if self.kind == KIND_MOWER:
            return state.state == "docked"
        return state.state in ("docked", "idle")

    # ---------------------------------------------------------------- run

    @callback
    def tick(self, _now: Any = None) -> None:
        """Check the conditions and announce / start."""
        local = dt_util.now()
        today = local.date().isoformat()
        st = self._state()
        before = (self.status, tuple(self.due), tuple(self.mop_due), self.start_at)
        self.due = self.due_areas() if self.is_on else []
        self.mop_due = self.mop_due_areas() if self.is_on else []
        if st.get("mop_pending"):
            self._mop_pass(st)
        elif not self.is_on:
            self.status, self.start_at = "off", None
        elif st.get("skip") == today:
            self.status, self.start_at = "skipped", None
        elif st.get("started") == today:
            self.status, self.start_at = "done_today", None
        elif not self.due and not self.mop_due:
            self.status, self.start_at = "nothing_due", None
        elif (reason := self.blocker(local)) is not None:
            self.status, self.start_at = reason, None
        elif self.start_at is None and int(self.settings.get("announce") or 0) > 0:
            self.start_at = now_ms() + int(self.settings["announce"]) * 60_000
            self.status = "announced"
            self._announce()
        elif self.start_at is not None and now_ms() < self.start_at:
            self.status = "announced"
        else:
            self._start(self.due)
            st["started"] = today
            self.status, self.start_at = "started", None
            self._changed(save=True)
        if (self.status, tuple(self.due), tuple(self.mop_due), self.start_at) != before:
            self._changed()

    def _mop_pass(self, st: dict[str, Any]) -> None:
        """Second pass: mop the rooms once vacuuming has finished and the robot is back."""
        pending = st["mop_pending"]
        now = now_ms()
        session = getattr(self.tracker, "_session", None)
        finished = session is None or session.get("end") is not None
        ended = (session or {}).get("end") or pending.get("at", now)
        if now - pending.get("at", now) > MOP_GIVE_UP_MS:
            st["mop_pending"] = None
            self.status = "done_today"
            self._changed(save=True)
            return
        if not (
            finished
            and self._ready()
            and now - ended >= MOP_WAIT_MS
            and now - pending["at"] >= MOP_WAIT_MS
        ):
            self.status = "mop_pending"
            return
        control = self.tracker.mop_control() if isinstance(self.tracker, VacuumTracker) else None
        entity = self.tracker.entity_id
        steps = []
        if control:
            steps.append(
                (
                    "select",
                    "select_option",
                    {"entity_id": control["entity"], "option": control["mop"]},
                )
            )
        steps.append(self._clean_step(entity, pending["rooms"]))
        self._record_run(pending["rooms"])
        self._call_seq(steps)
        st["mop_pending"] = None
        self.status = "mopping"
        self._changed(save=True)

    def _rooms(self) -> list[str]:
        """Return the rooms of the (first) run: due ones plus rooms to be mopped afterwards."""
        return [*self.due, *[a for a in self.mop_due if a not in self.due]]

    def _names(self, areas: list[str]) -> str:
        return ", ".join(self.area_name(a) for a in areas)

    def _announce(self) -> None:
        src = "gd.mower" if self.kind == KIND_MOWER else "hh.robot"
        cfg = (self.store.data["notify"].get("sources") or {}).get(src) or {}
        if not cfg.get("push"):
            return
        at = dt_util.as_local(dt_util.utc_from_timestamp((self.start_at or 0) / 1000))
        verb = "mäht" if self.kind == KIND_MOWER else "saugt"
        self.engine.send(
            "Mähroboter" if self.kind == KIND_MOWER else "Saugroboter",
            f"{self.tracker.name} {verb} ab {at:%H:%M}: {self._names(self._rooms())}"
            + (f" – danach wischen: {self._names(self.mop_due)}" if self.mop_due else ""),
            src=src,
            url=cfg.get("url") or "/",
            tag=f"planner|{self.id}",
            actions=[{"action": f"{ACTION_SKIP}{self.id}", "title": "Heute nicht"}],
        )

    def _start(self, areas: list[str]) -> None:
        entity = self.tracker.entity_id
        if not entity:
            return
        if isinstance(self.tracker, VacuumTracker):
            rooms = self._rooms()
            steps = []
            # with a mop: vacuum only first (more often); rooms due for mopping follow later
            if self.can_mop() and (control := self.tracker.mop_control()):
                steps.append(
                    (
                        "select",
                        "select_option",
                        {"entity_id": control["entity"], "option": control["vacuum"]},
                    )
                )
                if self.mop_due:
                    self._state()["mop_pending"] = {"rooms": list(self.mop_due), "at": now_ms()}
            steps.append(self._clean_step(entity, rooms))
            self._record_run(rooms)
            self._call_seq(steps)
            return
        switches: dict[str, str] = {}
        buttons: dict[str, str] = {}
        for entry in self.tracker.entities():
            state = self.hass.states.get(entry.entity_id)
            name = state.attributes.get("friendly_name", "") if state else ""
            if entry.domain == "switch" and entry.translation_key in ("area", "area_plain"):
                switches[area_name(self.tracker.name, name).lower()] = entry.entity_id
            elif entry.domain == "button" and entry.translation_key == "task":
                buttons[name.lower()] = entry.entity_id
        wanted = {a.lower() for a in areas}
        if switches:
            for name, switch in switches.items():
                self._call(
                    "switch", "turn_on" if name in wanted else "turn_off", {"entity_id": switch}
                )
            self._call("lawn_mower", "start_mowing", {"entity_id": entity})
            return
        button = next((b for n, b in buttons.items() if any(a in n for a in wanted)), None)
        if button:
            self._call("button", "press", {"entity_id": button})
        else:
            self._call("lawn_mower", "start_mowing", {"entity_id": entity})

    def _clean_step(self, entity: str | None, rooms: list[str]) -> tuple[str, str, dict[str, Any]]:
        if self.hass.services.has_service("vacuum", "clean_area"):
            return ("vacuum", "clean_area", {"entity_id": entity, "cleaning_area_id": rooms})
        return ("vacuum", "start", {"entity_id": entity})

    def _record_run(self, rooms: list[str]) -> None:
        """Tell the room tracking which rooms a run is for (needed without a room sensor)."""
        hh = self.store.data["data"].setdefault("household", {})
        now = now_ms()
        runs = [r for r in hh.get("runs") or [] if now - r.get("t", 0) < 14 * 86_400_000]
        self.store.set_data(
            {"household": {**hh, "runs": [*runs[-19:], {"t": now, "areas": rooms}]}}
        )

    def _call_seq(self, steps: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Call services one after the other (e.g. set the mop mode, then start)."""

        async def run() -> None:
            for domain, service, data in steps:
                try:
                    await self.hass.services.async_call(domain, service, data, blocking=True)
                except Exception:  # noqa: BLE001 - a failed step must not break the planner
                    _LOGGER.warning("Robot planner: %s.%s failed", domain, service, exc_info=True)

        self.hass.async_create_task(run())

    def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        async def run() -> None:
            try:
                await self.hass.services.async_call(domain, service, data, blocking=True)
            except Exception:  # noqa: BLE001 - a failed start must not break the planner
                _LOGGER.warning("Robot planner: %s.%s failed", domain, service, exc_info=True)

        self.hass.async_create_task(run())

    def _changed(self, save: bool = False) -> None:
        if save:
            self.store.save(SIGNAL_DATA)
        else:
            async_dispatcher_send(self.hass, SIGNAL_DATA)
        async_dispatcher_send(self.hass, SIGNAL_PLANNER, self.id)

    def info(self) -> dict[str, Any]:
        """Return the planner state for the dashboard."""
        return {
            "on": self.is_on,
            "status": self.status,
            "due": self.due,
            "mop_due": self.mop_due,
            "mop": self.can_mop(),
            "start_at": self.start_at,
            "switch": entity_id_for(self.hass, "switch", f"planner_{self.id}"),
        }


def entity_id_for(hass: HomeAssistant, domain: str, unique_suffix: str) -> str | None:
    """Find one of our entities by the end of its unique ID."""
    for entry in er.async_get(hass).entities.values():
        if (
            entry.domain == domain
            and entry.platform == "smarthome_ui"
            and entry.unique_id.endswith(unique_suffix)
        ):
            return entry.entity_id
    return None


class PlannerManager:
    """The planners of all robots."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, engine: NotifyEngine) -> None:
        """Initialize the manager."""
        self.hass = hass
        self.store = store
        self.engine = engine
        self.planners: dict[str, RobotPlanner] = {}
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_setup(
        self,
        subentries: list[ConfigSubentry],
        mowers: dict[str, MowerTracker],
        vacuums: dict[str, VacuumTracker],
    ) -> None:
        """Create a planner per mower and vacuum."""
        for subentry in subentries:
            if tracker := mowers.get(subentry.subentry_id):
                self.planners[tracker.id] = RobotPlanner(
                    self.hass, self.store, self.engine, tracker, KIND_MOWER, subentry
                )
            elif vac := vacuums.get(subentry.subentry_id):
                self.planners[vac.id] = RobotPlanner(
                    self.hass, self.store, self.engine, vac, KIND_VACUUM, subentry
                )
        if not self.planners:
            return

        @callback
        def action(event: Event) -> None:
            name = str(event.data.get("action") or "")
            if name.startswith(ACTION_SKIP) and (p := self.planners.get(name[len(ACTION_SKIP) :])):
                p.skip_today()

        self._unsub.append(self.hass.bus.async_listen("mobile_app_notification_action", action))
        self._unsub.append(async_track_time_interval(self.hass, self._tick, PLANNER_INTERVAL))
        self.hass.loop.call_soon(self._tick)

    @callback
    def _tick(self, _now: Any = None) -> None:
        for planner in self.planners.values():
            planner.tick()

    @callback
    def async_stop(self) -> None:
        """Stop planning."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    def info(self, robot_id: str) -> dict[str, Any] | None:
        """Return the planner state of one robot."""
        planner = self.planners.get(robot_id)
        return planner.info() if planner else None


class PlannerEntity(Entity):
    """An entity of the planner; belongs to the robot's own device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self, entry_id: str, planner: RobotPlanner, key: str, domain: str, obj: str
    ) -> None:
        """Initialize (entity ID: <domain>.<garden|household>_<obj>)."""
        self._planner = planner
        prefix = "garden" if planner.kind == KIND_MOWER else "household"
        self._attr_unique_id = f"{entry_id}_planner_{planner.id}{key}"
        self.entity_id = f"{domain}.{prefix}_{obj}"
        if identifiers := planner.tracker.device_identifiers():
            self._attr_device_info = DeviceInfo(identifiers=identifiers)

    async def async_added_to_hass(self) -> None:
        """Update when the planner changes."""
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_PLANNER, self._changed))

    @callback
    def _changed(self, robot_id: str) -> None:
        if robot_id == self._planner.id:
            self.async_write_ha_state()


@callback
def async_setup_planner_entities(
    hass: HomeAssistant,
    manager: PlannerManager,
    async_add_entities: AddConfigEntryEntitiesCallback,
    robot: Callable[[RobotPlanner], list[Entity]],
    per_area: Callable[[RobotPlanner, str], Entity | None] | None = None,
) -> CALLBACK_TYPE:
    """Add the planner entities of every robot (and per area as areas appear)."""
    for planner in manager.planners.values():
        async_add_entities(robot(planner), config_subentry_id=planner.id)
    if per_area is None:
        return lambda: None
    known: set[tuple[str, str]] = set()

    @callback
    def add_areas(_robot: str | None = None) -> None:
        for planner in manager.planners.values():
            new = [a for a in planner.areas() if (planner.id, a) not in known]
            if new:
                known.update((planner.id, a) for a in new)
                entities = [e for a in new if (e := per_area(planner, a)) is not None]
                if entities:
                    async_add_entities(entities, config_subentry_id=planner.id)

    add_areas()
    unsubs = [
        async_dispatcher_connect(hass, SIGNAL_MOWER, add_areas),
        async_dispatcher_connect(hass, SIGNAL_VACUUM, add_areas),
    ]

    @callback
    def unsub_all() -> None:
        for unsub in unsubs:
            unsub()

    return unsub_all
