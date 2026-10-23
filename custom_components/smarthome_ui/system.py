"""System and network: internet, network devices, services, status entities, power, automations.

What is watched comes from the dashboard (``system/settings``), the way its Netzwerk, Status and
Haustechnik pages are configured. Core checks every minute and notifies:

* **internet** (``net.internet``) – the connection entity is not online for 2 minutes,
* **network devices** (``net.devices``) – gateway, switches … not online for 5 minutes,
* **services** (``sys.services``) – an integration of a service fails (setup error, retrying,
  migration error) or its bridge entity (Zigbee2MQTT …) is offline; status entities of the pages
  report an error or are not reachable for 10 minutes,
* **power** (``sys.power``) – the Raspberry Pi reports under-voltage (``rpi_power``),
* **automations** (``auto.errors``) – the last run of an automation ended with an error.

Entity (device "System"): "Probleme" (sensor, the messages as attribute).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SIGNAL_DATA, SIGNAL_SYSTEM
from .entity import DEVICE_SYSTEM, CoreEntity
from .store import CoreStore

_LOGGER = logging.getLogger(__name__)

ONLINE = ("on", "home", "connected", "online", "true", "up", "verbunden")
ENTRY_ERRORS = {
    ConfigEntryState.SETUP_RETRY: "Verbindung wird wiederholt",
    ConfigEntryState.SETUP_ERROR: "Einrichtung fehlgeschlagen",
    ConfigEntryState.MIGRATION_ERROR: "Migrationsfehler",
    ConfigEntryState.FAILED_UNLOAD: "Fehler beim Entladen",
}
MINUTES = {"internet": 2, "device": 5, "status": 10, "service": 5}
_BENIGN = re.compile(
    r"(zu geringe dc|dc.?leistung.*gering|low dc|insufficient dc|schlaf|sleep|standby|nacht|night|"
    r"keine sonne|no sun)",
    re.IGNORECASE,
)
_NO_ERROR = re.compile(r"(^|[\s_-])(kein|keine|no|none)[\s_-]*(fehler|error|störung|fault)")
_ERROR = re.compile(
    r"fehler|error|fault|störung|alarm|failure|defekt|disconnected|offline|getrennt"
)


def _online(state: State) -> bool:
    if state.domain == "button":
        return state.state != "unavailable"
    return state.state.lower() in ONLINE


class System:
    """Watch the system and the network and tell about problems."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, notify: Callable[..., None]) -> None:
        """Initialize (notify(src, title, message, tag))."""
        self.hass = hass
        self.store = store
        self.notify = notify
        self._unsub: list[CALLBACK_TYPE] = []
        self._sent: set[str] = set()
        self._primed = False
        self._auto_seen: dict[str, str] = {}
        self.problems: dict[str, dict[str, str]] = {}

    @property
    def data(self) -> dict[str, Any]:
        """Return the settings from the dashboard."""
        return self.store.data["system"]

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Take what is watched from the dashboard."""
        for key in ("internet", "devices", "status", "services", "automations"):
            if key in settings:
                self.data[key] = settings[key]
        self.data["configured"] = True
        self.store.save(SIGNAL_SYSTEM, SIGNAL_DATA)
        self.hass.async_create_task(self.async_evaluate())

    # ---------------------------------------------------------------- setup

    @callback
    def async_setup(self) -> None:
        """Start the checks."""
        self._unsub.append(
            async_track_time_interval(self.hass, self.async_evaluate, timedelta(minutes=1))
        )
        self.hass.async_create_task(self.async_evaluate())

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    # ---------------------------------------------------------------- checks

    def _since(self, state: State, minutes: int) -> bool:
        return dt_util.utcnow() - state.last_changed >= timedelta(minutes=minutes)

    def _status_problem(self, item: dict[str, Any]) -> str | None:
        """Return what is wrong with a status entity (like the dashboard's status bar)."""
        state = self.hass.states.get(item.get("entity") or "")
        if state is None:
            return None
        name = item.get("name") or state.attributes.get("friendly_name") or state.entity_id
        if state.state in ("unavailable", "unknown") or (
            state.domain == "button" and state.state == "unavailable"
        ):
            if state.state == "unavailable" and self._since(state, MINUTES["status"]):
                return f"{name}: nicht erreichbar"
            return None
        if state.domain == "button":
            return None
        value = state.state.lower()
        if (ok := item.get("ok_states")) is not None:
            bad = state.state not in [str(x) for x in ok] and not _BENIGN.search(value)
        elif (errors := item.get("error_states")) is not None:
            bad = state.state in [str(x) for x in errors]
        elif state.domain == "binary_sensor":
            dc = state.attributes.get("device_class")
            bad = (dc == "connectivity" and value == "off") or (
                value == "on" and dc in ("problem", "safety", "smoke", "moisture")
            )
        else:
            bad = not (
                _NO_ERROR.search(value)
                or value in ("ok", "0", "none", "normal")
                or _BENIGN.search(value)
            ) and bool(_ERROR.search(value))
        if not bad:
            return None
        if (
            state.domain == "binary_sensor"
            and state.attributes.get("device_class") == "connectivity"
        ):
            return f"{name}: nicht erreichbar"
        return f"{name}: {state.state}"

    def _service_problem(self, svc: dict[str, Any]) -> str | None:
        name = svc.get("name") or ", ".join(svc.get("domains") or [])
        entries = [
            e
            for domain in svc.get("domains") or []
            for e in self.hass.config_entries.async_entries(domain)
            if not e.disabled_by
        ]
        # a not loaded old entry next to a running one (e.g. ZHA next to Zigbee2MQTT) does not count
        if any(e.state is ConfigEntryState.LOADED for e in entries):
            entries = [e for e in entries if e.state is not ConfigEntryState.NOT_LOADED]
        bad = next((e for e in entries if e.state in ENTRY_ERRORS), None)
        if bad is not None:
            return f"{name}: {bad.title or bad.domain}: {ENTRY_ERRORS[bad.state]}"
        bridge = self.hass.states.get(svc.get("entity") or "")
        if bridge is not None and not (
            _online(bridge) or bridge.state.lower() in ("running", "läuft", "online", "connected")
        ):
            if self._since(bridge, MINUTES["service"]):
                label = svc.get("entity_name") or bridge.attributes.get("friendly_name") or "Bridge"
                return f"{name}: {label}: {bridge.state}"
        return None

    def _rpi_power(self) -> State | None:
        registry = er.async_get(self.hass)
        for state in self.hass.states.async_all("binary_sensor"):
            entry = registry.async_get(state.entity_id)
            text = f"{state.entity_id} {state.attributes.get('friendly_name', '')}".lower()
            if (entry is not None and entry.platform == "rpi_power") or re.search(
                r"rpi_power|stromversorgungspr", text
            ):
                return state
        return None

    async def _automation_errors(self) -> dict[str, str]:
        """Return the automations whose last run failed ({entity_id: run id})."""
        if self.data.get("automations") is False:
            return {}
        try:
            from homeassistant.components.trace.util import async_list_traces
        except ImportError:
            return {}
        try:
            traces = await async_list_traces(self.hass, "automation", None)
        except Exception as err:  # noqa: BLE001 - traces are optional
            _LOGGER.debug("No automation traces: %s", err)
            return {}
        last: dict[str, dict[str, Any]] = {}
        for trace in traces:
            key = str(trace.get("item_id") or "")
            start = str((trace.get("timestamp") or {}).get("start") or "")
            if key and start >= str((last.get(key, {}).get("timestamp") or {}).get("start") or ""):
                last[key] = trace
        ids = {
            s.attributes.get("id"): s.entity_id for s in self.hass.states.async_all("automation")
        }
        out = {}
        for key, trace in last.items():
            failed = trace.get("script_execution") == "error" or (
                trace.get("state") == "stopped" and bool(trace.get("error"))
            )
            if failed and key in ids:
                out[ids[key]] = str(trace.get("run_id"))
        return out

    def _problems(self, auto: dict[str, str]) -> dict[str, dict[str, str]]:
        out: dict[str, dict[str, str]] = {}

        def add(key: str, src: str, title: str, message: str) -> None:
            out[key] = {"src": src, "title": title, "message": message}

        internet = self.hass.states.get(self.data.get("internet") or "")
        if (
            internet is not None
            and not _online(internet)
            and self._since(internet, MINUTES["internet"])
        ):
            add("internet", "net.internet", "Internet", "Internet: getrennt")
        for dev in self.data.get("devices") or []:
            state = self.hass.states.get(dev.get("entity") or "")
            if state is None or _online(state) or not self._since(state, MINUTES["device"]):
                continue
            name = dev.get("name") or state.attributes.get("friendly_name") or state.entity_id
            text = "nicht erreichbar" if state.state == "unavailable" else state.state
            add(f"dev|{state.entity_id}", "net.devices", "Netzwerk", f"{name}: {text}")
        for item in self.data.get("status") or []:
            if msg := self._status_problem(item):
                add(f"status|{item.get('entity')}", "sys.services", "System", msg)
        for svc in self.data.get("services") or []:
            if msg := self._service_problem(svc):
                add(f"svc|{svc.get('name')}", "sys.services", "Dienste", msg)
        if (power := self._rpi_power()) is not None and power.state == "on":
            add("power", "sys.power", "Raspberry Pi", "Raspberry Pi: Unterspannung")
        for entity_id, run in auto.items():
            state = self.hass.states.get(entity_id)
            name = state.attributes.get("friendly_name", entity_id) if state else entity_id
            add(
                f"auto|{entity_id}|{run}",
                "auto.errors",
                "Automationen",
                f"Automation „{name}“: Fehler beim letzten Lauf",
            )
        return out

    async def async_evaluate(self, _now: Any = None) -> None:
        """Check everything and send what is new."""
        if not self.data.get("configured"):
            return
        problems = self._problems(await self._automation_errors())
        if self._primed:
            for key, p in problems.items():
                if key not in self._sent:
                    tag = "system|" + "|".join(key.split("|")[:2])
                    self.notify(p["src"], p["title"], p["message"], tag)
        self._sent = set(problems)
        self._primed = True
        if problems != self.problems:
            self.problems = problems
            self.store.save(SIGNAL_SYSTEM, SIGNAL_DATA)

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        return {
            "configured": bool(self.data.get("configured")),
            "problems": [
                {"key": k, "src": p["src"], "message": p["message"]}
                for k, p in self.problems.items()
            ],
        }


class SystemProblems(CoreEntity, SensorEntity):
    """How many system and network problems there are now (the messages as attribute)."""

    _attr_translation_key = "system_problems"

    def __init__(self, entry_id: str, system: System) -> None:
        """Initialize (sensor.system_problems)."""
        super().__init__(entry_id, "system_problems", DEVICE_SYSTEM, "sensor", "problems")
        self._system = system

    @property
    def native_value(self) -> int:
        """Return the number of problems."""
        return len(self._system.problems)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the messages."""
        return {"problems": [p["message"] for p in self._system.problems.values()]}

    async def async_added_to_hass(self) -> None:
        """Update with the checks."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SYSTEM, self.async_write_ha_state)
        )
