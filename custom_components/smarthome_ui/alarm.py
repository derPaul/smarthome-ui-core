"""Alarm logic: which sensor triggers the alarm in which mode, reaction, guests, night walk.

This replaces the automations the dashboard used to write ("Alarm – Auslöser", "Alarm – Reaktion",
"Gäste", "Nachtweg", "Nachtweg – Rückfrage") and its helpers "Gäste da" / "Nachtweg bis". The
alarm panel itself (Manual, Alarmo …) stays; Core triggers it and reacts to it.

Settings come from the dashboard (``alarm/settings``), in the shape it edits them:

* ``panel`` – the ``alarm_control_panel``,
* ``rules`` – ``{sensor: [modes]}``: the sensor triggers the alarm in these modes,
* ``actions`` – reaction: ``targets`` (notify services), ``critical``, ``pending`` (push when the
  entry delay starts), ``clear`` (push when disarmed after an alarm), ``sirens``, ``lights``,
* ``guests`` – ``devices`` / ``ssids`` / ``unifi_guest`` mean guests are there (off ``away``
  minutes after the last left); then the ``skip`` sensors are silent in the guest ``modes``;
  ``push_arm`` – tell when it is armed with guests,
* ``nightwalk`` – motion at the ``starts`` sensors (upstairs) opens a window of ``window`` minutes
  in which motion at the ``affected`` sensors (downstairs) is no burglary – only if the ``below``
  sensors were quiet for ``quiet`` minutes before; ``effect`` ``ask``: push "bist du das?" and
  trigger the alarm after ``ask_delay`` seconds without answer.

Entities (device "Sicherheit"): "Alarm-Logik" (switch, off = Core does not trigger), "Gäste da"
(switch), "Nachtweg bis" (sensor), "Nachtweg öffnen" (button), "Alarm-Grund" (sensor).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_ALARM
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

ASK_OK = "SH_NACHTWEG_OK"
ASK_ALARM = "SH_NACHTWEG_ALARM"
# automations the dashboard wrote before (and the older package ones)
OLD_AUTOMATIONS = (
    "smarthome_alarm_regeln",
    "smarthome_alarm_reaktion",
    "smarthome_gaeste",
    "smarthome_nachtweg",
    "smarthome_nachtweg_frage",
    "smarthome_alarm_ausloesen",
    "smarthome_alarm_benachrichtigen",
)
GUEST_MODES_DEFAULT = ["armed_home", "armed_away", "armed_night", "armed_vacation"]


class AlarmLogic:
    """The alarm rules, reaction, guests and night walk."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self._unsub: list[CALLBACK_TYPE] = []
        self._guest_timer: CALLBACK_TYPE | None = None
        self._asking: asyncio.Task | None = None

    # ---------------------------------------------------------------- settings and state

    @property
    def data(self) -> dict[str, Any]:
        """Return settings and state."""
        return self.store.data["alarm"]

    @property
    def configured(self) -> bool:
        """Return True once the dashboard handed the alarm over."""
        return bool(self.data.get("configured") and self.panel)

    @property
    def enabled(self) -> bool:
        """Return True if Core runs the alarm logic."""
        return self.configured and self.data.get("enabled", True)

    @property
    def panel(self) -> str | None:
        """Return the alarm panel."""
        return self.data.get("panel") or None

    def rules(self) -> dict[str, list[str]]:
        """Return {sensor: [modes]}."""
        return {e: list(m or []) for e, m in (self.data.get("rules") or {}).items()}

    def actions(self) -> dict[str, Any]:
        """Return the reaction settings."""
        base = {"targets": [], "critical": True, "pending": False, "clear": True}
        return {**base, "sirens": [], "lights": False, **(self.data.get("actions") or {})}

    def guests(self) -> dict[str, Any]:
        """Return the guest settings."""
        base = {"devices": [], "ssids": [], "unifi_guest": False, "away": 30, "push_arm": True}
        return {**base, "modes": GUEST_MODES_DEFAULT, "skip": [], **(self.data.get("guests") or {})}

    def nightwalk(self) -> dict[str, Any]:
        """Return the night walk settings."""
        base = {"enabled": False, "starts": [], "affected": [], "below": [], "window": 3}
        extra = {"quiet": 5, "modes": ["armed_night"], "effect": "ask", "ask_delay": 60}
        return {**base, **extra, **(self.data.get("nightwalk") or {})}

    @property
    def guests_on(self) -> bool:
        """Return True while guests are there."""
        return bool(self.data.get("guests_on"))

    @property
    def nightwalk_until(self) -> int:
        """Return until when the night walk is open (ms, 0 = closed)."""
        until = int(self.data.get("nightwalk_until") or 0)
        return until if until > now_ms() else 0

    def mode(self) -> str | None:
        """Return the current state of the panel."""
        state = self.hass.states.get(self.panel or "")
        return state.state if state else None

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Take the settings from the dashboard (and the current helper values once)."""
        for key in ("panel", "rules", "actions", "guests", "nightwalk"):
            if key in settings:
                self.data[key] = settings[key]
        if "guests_on" in settings and settings["guests_on"] is not None:
            self.data["guests_on"] = bool(settings["guests_on"])
        if settings.get("nightwalk_until"):
            self.data["nightwalk_until"] = int(settings["nightwalk_until"])
        self.data["configured"] = True
        self.data.setdefault("enabled", True)
        self.store.save()
        if self.enabled:
            self._old_automations(False)
        self._listen()
        self._check_guests()
        async_dispatcher_send(self.hass, SIGNAL_ALARM)

    @callback
    def set_enabled(self, enabled: bool) -> None:
        """Run the alarm logic in Core or not (the dashboard's automations would be needed then)."""
        self.data["enabled"] = enabled
        self.store.save()
        # never both and never none: the old automations run while Core does not
        self._old_automations(not enabled)
        async_dispatcher_send(self.hass, SIGNAL_ALARM)

    def old_automations(self) -> list[str]:
        """Return the entity IDs of the dashboard's former alarm automations."""
        return [
            s.entity_id
            for s in self.hass.states.async_all("automation")
            if s.attributes.get("id") in OLD_AUTOMATIONS
        ]

    @callback
    def _old_automations(self, on: bool) -> None:
        """Switch the dashboard's former alarm automations on / off (they are kept)."""
        if entities := self.old_automations():
            self.hass.async_create_task(
                self._call("automation", "turn_on" if on else "turn_off", entities)
            )

    @callback
    def set_guests(self, on: bool) -> None:
        """Guests there or not (by hand; the detection changes it again on the next change)."""
        if self.guests_on != on:
            self.data["guests_on"] = on
            self.store.save()
            async_dispatcher_send(self.hass, SIGNAL_ALARM)

    @callback
    def open_nightwalk(self, reason: str = "von Hand") -> None:
        """Open the night walk window now."""
        minutes = float(self.nightwalk()["window"] or 3)
        self.data["nightwalk_until"] = now_ms() + int(minutes * 60_000)
        self.store.save()
        self._log(f"Nachtweg geöffnet ({reason})")
        async_dispatcher_send(self.hass, SIGNAL_ALARM)

    # ---------------------------------------------------------------- setup

    @callback
    def async_setup(self) -> None:
        """Start listening."""
        self._listen()
        self._check_guests()

    @callback
    def async_stop(self) -> None:
        """Stop listening."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._guest_timer:
            self._guest_timer()
            self._guest_timer = None
        if self._asking and not self._asking.done():
            self._asking.cancel()

    @callback
    def _listen(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        if not self.configured:
            return
        sensors = set(self.rules())
        starts = set(self.nightwalk()["starts"] or [])
        panel = self.panel

        @callback
        def relevant(data: EventStateChangedData) -> bool:
            eid = data["entity_id"]
            return (
                eid in sensors or eid in starts or eid == panel or eid.startswith("device_tracker.")
            )

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            eid = event.data["entity_id"]
            old, new = event.data["old_state"], event.data["new_state"]
            if new is None:
                return
            if eid.startswith("device_tracker."):
                self._check_guests()
                return
            if eid == panel:
                if old is not None and old.state != new.state:
                    self.hass.async_create_task(self._async_react(old.state, new.state))
                return
            turned_on = new.state == "on" and (old is None or old.state != "on")
            if not turned_on:
                return
            if eid in starts:
                self._nightwalk_start(eid)
            if eid in sensors:
                self._sensor_on(eid)

        self._unsub.append(
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed, event_filter=relevant)
        )

    # ---------------------------------------------------------------- triggering

    def _name(self, entity_id: str) -> str:
        state = self.hass.states.get(entity_id)
        return str(state.attributes.get("friendly_name") or entity_id) if state else entity_id

    @callback
    def _log(self, message: str) -> None:
        # a logbook entry (what logbook.log does, without depending on the logbook integration)
        data = {"name": "Alarmanlage", "message": message, "domain": DOMAIN}
        if self.panel:
            data["entity_id"] = self.panel
        self.hass.bus.async_fire("logbook_entry", data)

    @callback
    def _sensor_on(self, entity_id: str) -> None:
        """A sensor turned on: trigger the alarm if it counts in the current mode."""
        if not self.enabled:
            return
        mode = self.mode()
        if mode is None or mode not in self.rules().get(entity_id, []):
            return
        name = self._name(entity_id)
        guests = self.guests()
        if self.guests_on and mode in guests["modes"] and entity_id in guests["skip"]:
            self._log(f"{name}: wegen Gästen stumm")
            return
        walk = self.nightwalk()
        if (
            walk["enabled"]
            and mode in walk["modes"]
            and entity_id in walk["affected"]
            and self.nightwalk_until
        ):
            if walk["effect"] == "ask" and self.actions()["targets"]:
                if self._asking is None or self._asking.done():
                    self._asking = self.hass.async_create_task(self._async_ask(name))
            else:
                self._log(f"Nachtweg: {name} nicht als Einbruch gewertet")
            return
        self.hass.async_create_task(self._async_trigger(entity_id, name))

    async def _async_trigger(self, entity_id: str | None, name: str) -> None:
        self.data["reason"] = {"entity": entity_id, "name": name, "t": now_ms()}
        self.store.save()
        async_dispatcher_send(self.hass, SIGNAL_ALARM)
        try:
            await self.hass.services.async_call(
                "alarm_control_panel",
                "alarm_trigger",
                {"entity_id": self.panel},
                blocking=True,
            )
        except HomeAssistantError as err:
            _LOGGER.error("Could not trigger %s: %s", self.panel, err)
        self._log(f"ausgelöst durch {name}")

    # ---------------------------------------------------------------- night walk

    @callback
    def _nightwalk_start(self, entity_id: str) -> None:
        """Motion upstairs: open the window if downstairs was quiet before."""
        walk = self.nightwalk()
        if not self.enabled or not walk["enabled"] or self.mode() not in walk["modes"]:
            return
        if not self.nightwalk_until:
            quiet = timedelta(minutes=float(walk["quiet"] or 0))
            since = dt_util.utcnow() - quiet
            for below in walk["below"]:
                state = self.hass.states.get(below)
                if state is not None and state.last_changed > since:
                    return  # downstairs was not quiet: no night walk
        self.open_nightwalk(self._name(entity_id))

    async def _async_ask(self, name: str) -> None:
        """Ask by push "is it you?"; trigger the alarm without answer."""
        walk = self.nightwalk()
        delay = float(walk["ask_delay"] or 60)
        message = f"{name} – bist du das? Sonst Alarm in {round(delay)} Sekunden."
        data = {
            "tag": "smarthome_nachtweg",
            "push": {"interruption-level": "time-sensitive"},
            "ttl": 0,
            "priority": "high",
            "actions": [
                {"action": ASK_OK, "title": "Ich bin's"},
                {"action": ASK_ALARM, "title": "Alarm auslösen", "destructive": True},
            ],
        }
        loop = asyncio.get_running_loop()
        answer: asyncio.Future[str] = loop.create_future()

        @callback
        def on_action(event: Event) -> None:
            action = event.data.get("action")
            if action in (ASK_OK, ASK_ALARM) and not answer.done():
                answer.set_result(action)

        @callback
        def on_panel(event: Event[EventStateChangedData]) -> None:
            new = event.data["new_state"]
            if event.data["entity_id"] == self.panel and new is not None:
                if new.state == "disarmed" and not answer.done():
                    answer.set_result("disarmed")

        unsub = [
            self.hass.bus.async_listen("mobile_app_notification_action", on_action),
            self.hass.bus.async_listen(EVENT_STATE_CHANGED, on_panel),
        ]
        try:
            await self._push("🌙 Bewegung unten", message, data)
            try:
                result = await asyncio.wait_for(answer, timeout=delay)
            except TimeoutError:
                result = "timeout"
        finally:
            for item in unsub:
                item()
        if result == ASK_OK:
            self.open_nightwalk(f"bestätigt: {name}")
        elif result != "disarmed" and self.mode() in walk["modes"]:
            await self._async_trigger(None, f"{name} (Nachtweg ohne Bestätigung)")

    # ---------------------------------------------------------------- guests

    def guests_present(self) -> list[str]:
        """Return the guest devices at home."""
        guests = self.guests()
        out = [
            d
            for d in guests["devices"]
            if (s := self.hass.states.get(d)) is not None and s.state == "home"
        ]
        for state in self.hass.states.async_all("device_tracker"):
            if state.state != "home" or state.entity_id in out:
                continue
            attrs = state.attributes
            if (guests["ssids"] and attrs.get("essid") in guests["ssids"]) or (
                guests["unifi_guest"] and attrs.get("is_guest") is True
            ):
                out.append(state.entity_id)
        return out

    @callback
    def _check_guests(self) -> None:
        """Guests come: on at once; all gone: off after the away time."""
        guests = self.guests()
        if not self.configured or not (
            guests["devices"] or guests["ssids"] or guests["unifi_guest"]
        ):
            return
        if self.guests_present():
            if self._guest_timer:
                self._guest_timer()
                self._guest_timer = None
            self.set_guests(True)
            return
        if not self.guests_on or self._guest_timer:
            return

        @callback
        def gone(_now: Any) -> None:
            self._guest_timer = None
            if not self.guests_present():
                self.set_guests(False)

        self._guest_timer = async_call_later(
            self.hass, timedelta(minutes=float(guests["away"] or 30)), gone
        )

    # ---------------------------------------------------------------- reaction

    def reason(self) -> str:
        """Return what triggered the alarm (recorded, else the last sensor that turned on)."""
        rec = self.data.get("reason") or {}
        if rec.get("t") and now_ms() - int(rec["t"]) < 3_600_000:
            return str(rec.get("name") or "unbekannt")
        states = [
            s
            for e in self.rules()
            if (s := self.hass.states.get(e)) is not None and self.rules()[e]
        ]
        states.sort(key=lambda s: s.last_changed, reverse=True)
        on = [s for s in states if s.state == "on"] or states
        return str(on[0].attributes.get("friendly_name") or on[0].entity_id) if on else "unbekannt"

    async def _push(self, title: str, message: str, data: dict[str, Any]) -> None:
        for target in self.actions()["targets"]:
            try:
                await self.hass.services.async_call(
                    "notify",
                    target,
                    {"title": title, "message": message, "data": data},
                    blocking=True,
                )
            except (HomeAssistantError, ValueError) as err:
                _LOGGER.warning("Alarm push to notify.%s failed: %s", target, err)

    async def _call(self, domain: str, service: str, entities: list[str], **data: Any) -> None:
        if not entities:
            return
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": entities, **data}, blocking=True
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("%s.%s failed: %s", domain, service, err)

    async def _async_react(self, old: str, new: str) -> None:
        """React to the panel: alarm, entry delay, end of the alarm, armed with guests."""
        if not self.enabled:
            return
        act = self.actions()
        sirens = [s for s in act["sirens"] if s.startswith("siren.")]
        switches = [s for s in act["sirens"] if not s.startswith("siren.")]
        high = {"ttl": 0, "priority": "high"}
        if new == "triggered":
            grund = self.reason()
            at = dt_util.now().strftime("%H:%M")
            text = f"Ausgelöst durch {grund} um {at} Uhr"
            await self.hass.services.async_call(
                "persistent_notification",
                "create",
                {
                    "title": "🚨 Alarm ausgelöst",
                    "message": text,
                    "notification_id": "smarthome_alarm",
                },
                blocking=True,
            )
            if act["critical"]:
                level = {
                    "push": {
                        "interruption-level": "critical",
                        "sound": {"name": "default", "critical": 1, "volume": 1.0},
                    },
                    **high,
                    "channel": "alarm_stream",
                }
            else:
                level = {"push": {"interruption-level": "time-sensitive"}, **high}
            await self._push("🚨 Alarm ausgelöst", text, level)
            await self._call("siren", "turn_on", sirens)
            await self._call("homeassistant", "turn_on", switches)
            if act["lights"]:
                await self._call("light", "turn_on", ["all"], brightness_pct=100)
            return
        if new == "pending" and act["pending"]:
            await self._push(
                "⚠️ Eingangsverzögerung",
                f"{self.reason()} – Alarm startet gleich, jetzt entschärfen",
                {"push": {"interruption-level": "time-sensitive"}, **high},
            )
            return
        if old == "triggered":
            await self._call("siren", "turn_off", sirens)
            await self._call("homeassistant", "turn_off", switches)
            if act["clear"] and new == "disarmed":
                await self._push(
                    "✅ Alarm beendet",
                    "Die Alarmanlage wurde entschärft.",
                    {"push": {"interruption-level": "active"}, **high},
                )
            return
        guests = self.guests()
        if new in guests["modes"] and self.guests_on and guests["push_arm"] and guests["skip"]:
            rules = self.rules()
            n = sum(
                1 for e in guests["skip"] if any(m in guests["modes"] for m in rules.get(e, []))
            )
            await self._push(
                "👥 Gäste da",
                f"Alarmanlage scharf – {n} {'Sensor ist' if n == 1 else 'Sensoren sind'} "
                "wegen Gästen stumm.",
                {"push": {"interruption-level": "active"}, **high},
            )

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        rec = self.data.get("reason") or {}
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "panel": self.panel,
            "guests_on": self.guests_on,
            "guests_present": self.guests_present() if self.configured else [],
            "nightwalk_until": self.nightwalk_until or None,
            "reason": rec or None,
            "settings": {
                "rules": self.rules(),
                "actions": self.actions(),
                "guests": self.guests(),
                "nightwalk": self.nightwalk(),
            }
            if self.configured
            else None,
        }
