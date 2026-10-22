"""Presence simulation: lights on and off at random in the evening, covers at dusk and dawn.

This replaces the automation the dashboard wrote ("Smarthome: Anwesenheit simulieren"). Settings
come from the dashboard (``presence_sim/settings``), in the shape it edits them:

* ``lights`` / ``covers`` – what is switched,
* ``start`` – ``sunset`` (20 minutes before) or ``time`` (``from``); ``until`` – end of the evening,
* ``min`` / ``max`` – how long a light stays on (minutes), ``parallel`` – at most this many at once,
* ``when`` – ``away`` (nobody home), ``alarm`` (armed away / vacation) or ``always``,
* ``covers_open`` – when the covers open again in the morning (closed 10 minutes after sunset).

Every 5 minutes, in the evening window and while the condition holds, a light that is off is
switched on with a chance of 45 % (if fewer than ``parallel`` are on) and off again after a random
time. At ``until`` the lights it switched on go off. Covers move with a random delay.

Entity (device "Sicherheit"): "Anwesenheit simulieren" (switch).
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import random
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import (
    async_call_later,
    async_track_sunset,
    async_track_time_change,
    async_track_time_interval,
)
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from .const import SIGNAL_DATA, SIGNAL_PRESENCE_SIM
from .security import _hm
from .store import CoreStore

_LOGGER = logging.getLogger(__name__)

OLD_AUTOMATION = "smarthome_anwesenheit"
DEFAULTS: dict[str, Any] = {
    "enabled": False,
    "lights": [],
    "covers": [],
    "start": "sunset",
    "from": "18:00",
    "until": "23:30",
    "min": 10,
    "max": 40,
    "parallel": 2,
    "when": "away",
    "covers_open": "07:30",
}
CHANCE = 0.45
TICK = timedelta(minutes=5)


class PresenceSim:
    """Simulate that somebody is home."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, alarm_panel: Any) -> None:
        """Initialize (alarm_panel(): the alarm panel or None)."""
        self.hass = hass
        self.store = store
        self.alarm_panel = alarm_panel
        self._unsub: list[CALLBACK_TYPE] = []
        self._ours: dict[str, CALLBACK_TYPE] = {}  # lights switched on by us: their off timer

    # ---------------------------------------------------------------- settings

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored settings."""
        return self.store.data["presence_sim"]

    def settings(self) -> dict[str, Any]:
        """Return the settings with defaults."""
        return {**DEFAULTS, **self.data}

    @property
    def configured(self) -> bool:
        """Return True once lights or covers are chosen."""
        s = self.settings()
        return bool(s["lights"] or s["covers"])

    @property
    def enabled(self) -> bool:
        """Return True if the simulation is on."""
        return self.configured and bool(self.settings()["enabled"])

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Take the settings from the dashboard."""
        for key in DEFAULTS:
            if key in settings and settings[key] is not None:
                self.data[key] = settings[key]
        self._apply()

    @callback
    def set_enabled(self, enabled: bool) -> None:
        """Switch the simulation on / off."""
        self.data["enabled"] = enabled
        self._apply()

    @callback
    def _apply(self) -> None:
        if self.enabled:
            self._old_automation_off()
        else:
            self._lights_off()
        self._schedule()
        self.store.save(SIGNAL_PRESENCE_SIM, SIGNAL_DATA)

    @callback
    def _old_automation_off(self) -> None:
        old = [
            s.entity_id
            for s in self.hass.states.async_all("automation")
            if s.attributes.get("id") == OLD_AUTOMATION and s.state == "on"
        ]
        if old:
            self.hass.async_create_task(self._call("automation", "turn_off", old))

    # ---------------------------------------------------------------- condition and window

    def condition(self) -> bool:
        """Return True if the simulation may run now (nobody home / alarm armed / always)."""
        when = self.settings()["when"]
        panel = self.alarm_panel()
        if when == "alarm" and panel:
            state = self.hass.states.get(panel)
            return state is not None and state.state in ("armed_away", "armed_vacation")
        if when == "always":
            return True
        return not any(s.state == "home" for s in self.hass.states.async_all("person"))

    def window(self, now: datetime | None = None) -> tuple[datetime, datetime]:
        """Return start and end of this evening's window (local time)."""
        now = dt_util.as_local(now or dt_util.now())
        s = self.settings()
        uh, um = _hm(s["until"], "23:30")
        end = now.replace(hour=uh, minute=um, second=0, microsecond=0)
        start = None
        if s["start"] == "sunset":
            sunset = get_astral_event_date(self.hass, "sunset", now.date())
            if sunset is not None:
                start = dt_util.as_local(sunset) - timedelta(minutes=20)
        if start is None:
            fh, fm = _hm(s["from"], "18:00")
            start = now.replace(hour=fh, minute=fm, second=0, microsecond=0)
        return start, end

    def in_window(self, now: datetime | None = None) -> bool:
        """Return True in the evening window."""
        now = dt_util.as_local(now or dt_util.now())
        start, end = self.window(now)
        return start <= now < end

    def state(self) -> str:
        """Return off / waiting (condition not met) / ready (before the window) / running."""
        if not self.enabled:
            return "off"
        if not self.condition():
            return "waiting"
        return "running" if self.in_window() else "ready"

    # ---------------------------------------------------------------- running

    @callback
    def async_setup(self) -> None:
        """Start."""
        self._schedule()

    @callback
    def async_stop(self) -> None:
        """Stop (the lights stay as they are)."""
        for unsub in [*self._unsub, *self._ours.values()]:
            unsub()
        self._unsub.clear()
        self._ours.clear()

    @callback
    def _schedule(self) -> None:
        for unsub in self._unsub:
            unsub()
        self._unsub = []
        if not self.enabled:
            return
        s = self.settings()
        uh, um = _hm(s["until"], "23:30")
        self._unsub.append(async_track_time_interval(self.hass, self._tick, TICK))
        self._unsub.append(
            async_track_time_change(
                self.hass, lambda _n: self._lights_off(), hour=uh, minute=um, second=0
            )
        )
        if s["covers"]:
            oh, om = _hm(s["covers_open"], "07:30")
            self._unsub.append(
                async_track_sunset(
                    self.hass, lambda: self._covers("close", 20), timedelta(minutes=10)
                )
            )
            self._unsub.append(
                async_track_time_change(
                    self.hass, lambda _n: self._covers("open", 25), hour=oh, minute=om, second=0
                )
            )

    @callback
    def _tick(self, _now: Any = None) -> None:
        if not self.enabled or not self.condition() or not self.in_window():
            return
        s = self.settings()
        lights = [e for e in s["lights"] if self.hass.states.get(e) is not None]
        on = [e for e in lights if self.hass.states.get(e).state == "on"]
        if len(on) >= int(s["parallel"] or 2) or random.random() >= CHANCE:
            return
        off = [e for e in lights if e not in on]
        if not off:
            return
        light = random.choice(off)
        low = int(s["min"] or 10)
        minutes = random.randint(low, max(low, int(s["max"] or 40)))
        self.hass.async_create_task(self._call("light", "turn_on", [light]))

        @callback
        def done(_now: Any) -> None:
            self._ours.pop(light, None)
            self.hass.async_create_task(self._call("light", "turn_off", [light]))

        if old := self._ours.pop(light, None):
            old()
        self._ours[light] = async_call_later(self.hass, minutes * 60, done)

    @callback
    def _lights_off(self) -> None:
        lights = list(self._ours)
        for unsub in self._ours.values():
            unsub()
        self._ours.clear()
        if lights:
            self.hass.async_create_task(self._call("light", "turn_off", lights))

    @callback
    def _covers(self, action: str, spread: int) -> None:
        if not self.enabled or not self.condition():
            return
        covers = list(self.settings()["covers"])

        @callback
        def move(_now: Any) -> None:
            if self.enabled and self.condition():
                self.hass.async_create_task(self._call("cover", f"{action}_cover", covers))

        async_call_later(self.hass, random.randint(0, spread) * 60, move)

    async def _call(self, domain: str, service: str, entities: list[str]) -> None:
        try:
            await self.hass.services.async_call(
                domain, service, {"entity_id": entities}, blocking=True
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning("%s.%s failed: %s", domain, service, err)

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        start, end = self.window()
        return {
            "configured": self.configured,
            "enabled": self.enabled,
            "state": self.state(),
            "settings": self.settings(),
            "window": [int(start.timestamp() * 1000), int(end.timestamp() * 1000)],
            "lights_on": list(self._ours),
        }
