"""Holiday mode: one switch for the time everybody is away for days.

On (``switch.security_vacation`` or the dashboard):

* **heating** – every climate entity that heats is set ``offset`` K lower (default 3), as well as
  the day setpoints of a heat pump's heating circuits (numbers like "Tagbetrieb Temperatursoll
  HK 1"); hot water down to ``hot_water`` °C (default 40) if its comfort setpoint is found,
* **presence simulation** on (if set up),
* **alarm** armed away (if Core runs the alarm and a panel is set),
* **notifications**: only what needs you even from afar (security, water, fridge, frost, internet,
  power) – the others are counted.

With a return date (``until``) Core puts heating and hot water back ``preheat_h`` hours before
(default 24 – a floor heating needs that long). The mode ends when switched off or when someone
comes home; then everything comes back and a push sums up the time away.

Stored in ``vacation``: settings, ``on``, ``since``, ``prev`` (the values before), ``preheated``,
``suppressed`` and ``sim_was``.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import re
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.event import async_track_time_interval

from .const import SIGNAL_DATA, SIGNAL_VACATION
from .notify import NotifyEngine
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "offset": 3.0,
    "hot_water": 40.0,
    "sim": True,
    "alarm": True,
    "quiet": True,
    "until": None,
    "preheat_h": 24,
}
# what still comes as a push while away
ALLOW = ("sec.", "ht.water", "hh.fridge", "gd.frost", "net.internet", "sys.power")
_DAY_SETPOINT = re.compile(
    r"(tagbetrieb|komfortbetrieb|comfort|day).*temperatursoll.*hk|hk.*(tag|komfort).*soll"
)
_DHW_SETPOINT = re.compile(
    r"(tagbetrieb|komfortbetrieb|comfort).*warmwasser.*soll|warmwasser.*(komfort|tag).*soll"
)
TICK_MINUTES = 15


class Vacation:
    """Holiday mode."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        engine: NotifyEngine,
        presence_sim: Any,
        alarm: Any,
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self.engine = engine
        self.sim = presence_sim
        self.alarm = alarm
        self._unsub: list[CALLBACK_TYPE] = []
        engine.vacation_allows = self.allows

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored state."""
        return self.store.data.setdefault("vacation", {})

    @property
    def on(self) -> bool:
        """Return True while the holiday mode is on."""
        return bool(self.data.get("on"))

    def settings(self) -> dict[str, Any]:
        """Return the settings with defaults."""
        return {
            **DEFAULTS,
            **{k: v for k, v in (self.data.get("settings") or {}).items() if k in DEFAULTS},
        }

    @callback
    def async_setup(self) -> None:
        """Watch for somebody coming home and the return date."""

        @callback
        def changed(event: Event) -> None:
            eid = event.data.get("entity_id", "")
            new = event.data.get("new_state")
            if self.on and eid.startswith("person.") and new is not None and new.state == "home":
                self.hass.async_create_task(self.async_set(False, reason="home"))

        self._unsub.append(self.hass.bus.async_listen(EVENT_STATE_CHANGED, changed))
        self._unsub.append(
            async_track_time_interval(self.hass, self._tick, timedelta(minutes=TICK_MINUTES))
        )

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    # ---------------------------------------------------------------- notifications

    def allows(self, src: str) -> bool:
        """Return True if a notification of this source goes out (always when not on holiday)."""
        if not self.on or not self.settings()["quiet"]:
            return True
        if any(src.startswith(a) for a in ALLOW):
            return True
        self.data["suppressed"] = int(self.data.get("suppressed") or 0) + 1
        return False

    # ---------------------------------------------------------------- heating

    def _climates(self) -> list[str]:
        out = []
        for st in self.hass.states.async_all("climate"):
            temp = st.attributes.get("temperature")
            if st.state in ("heat", "auto", "heat_cool") and isinstance(temp, (int, float)):
                out.append(st.entity_id)
        return out

    def _numbers(self, pattern: re.Pattern[str], exclude: str | None = None) -> list[str]:
        out = []
        for st in self.hass.states.async_all("number"):
            text = f"{st.entity_id} {st.attributes.get('friendly_name', '')}".lower()
            if not pattern.search(text) or (exclude and exclude in text):
                continue
            try:
                float(st.state)
            except ValueError:
                continue
            out.append(st.entity_id)
        return out

    async def _call(self, domain: str, service: str, data: dict[str, Any]) -> None:
        try:
            await self.hass.services.async_call(domain, service, data, blocking=True)
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.warning(
                "Urlaubsmodus: %s.%s für %s ging nicht: %s",
                domain,
                service,
                data.get("entity_id"),
                err,
            )

    async def _lower(self) -> None:
        cfg = self.settings()
        prev: dict[str, float] = {}
        for eid in self._climates():
            st = self.hass.states.get(eid)
            temp = float(st.attributes["temperature"])
            low = max(float(st.attributes.get("min_temp", 5)), temp - float(cfg["offset"]))
            prev[eid] = temp
            await self._call("climate", "set_temperature", {"entity_id": eid, "temperature": low})
        for eid in self._numbers(_DAY_SETPOINT, exclude="warmwasser"):
            st = self.hass.states.get(eid)
            val = float(st.state)
            prev[eid] = val
            low = max(float(st.attributes.get("min", 5)), val - float(cfg["offset"]))
            await self._call("number", "set_value", {"entity_id": eid, "value": low})
        if cfg["hot_water"]:
            for eid in self._numbers(_DHW_SETPOINT):
                st = self.hass.states.get(eid)
                val = float(st.state)
                if val > float(cfg["hot_water"]):
                    prev[eid] = val
                    await self._call(
                        "number", "set_value", {"entity_id": eid, "value": float(cfg["hot_water"])}
                    )
        self.data["prev"] = prev

    async def _restore(self) -> None:
        for eid, val in (self.data.get("prev") or {}).items():
            if eid.startswith("climate."):
                await self._call(
                    "climate", "set_temperature", {"entity_id": eid, "temperature": val}
                )
            elif eid.startswith("number."):
                await self._call("number", "set_value", {"entity_id": eid, "value": val})
        self.data["prev"] = {}

    @callback
    def _tick(self, _now: Any = None) -> None:
        cfg = self.settings()
        until = cfg.get("until")
        if not self.on or not until or self.data.get("preheated"):
            return
        if now_ms() >= int(until) - int(cfg["preheat_h"]) * 3_600_000:
            self.data["preheated"] = True
            self.hass.async_create_task(self._async_preheat())

    async def _async_preheat(self) -> None:
        await self._restore()
        self._save()
        self.engine.send(
            "Urlaubsmodus",
            "Heizung und Warmwasser laufen wieder normal – bis zur Rückkehr ist es warm.",
            src="sec.vacation",
            tag="vacation",
        )

    # ---------------------------------------------------------------- on / off

    @callback
    def set_settings(self, settings: dict[str, Any]) -> None:
        """Change the settings (also while on)."""
        own = {**(self.data.get("settings") or {})}
        for key, value in settings.items():
            if key in DEFAULTS:
                own[key] = value
        self.data["settings"] = own
        if "until" in settings:
            self.data["preheated"] = False
        self._save()

    async def async_set(
        self, on: bool, settings: dict[str, Any] | None = None, reason: str = ""
    ) -> None:
        """Switch the holiday mode on or off."""
        if settings:
            self.set_settings(settings)
        if on == self.on:
            return
        cfg = self.settings()
        if on:
            self.data.update({"on": True, "since": now_ms(), "suppressed": 0, "preheated": False})
            await self._lower()
            if cfg["sim"] and getattr(self.sim, "configured", False):
                self.data["sim_was"] = bool(self.sim.enabled)
                self.sim.set_enabled(True)
            if cfg["alarm"] and getattr(self.alarm, "panel", None):
                st = self.hass.states.get(self.alarm.panel)
                if st is not None and st.state == "disarmed":
                    await self._call(
                        "alarm_control_panel", "alarm_arm_away", {"entity_id": self.alarm.panel}
                    )
            self._save()
            return
        since = int(self.data.get("since") or now_ms())
        if not self.data.get("preheated"):
            await self._restore()
        if "sim_was" in self.data and getattr(self.sim, "configured", False):
            self.sim.set_enabled(bool(self.data.pop("sim_was")))
        summary = self._summary(since)
        self.data.update({"on": False, "since": None, "preheated": False})
        # one-off return date
        own = self.data.get("settings") or {}
        own.pop("until", None)
        self.data["settings"] = own
        self._save()
        self.engine.send(
            "Willkommen zurück" if reason == "home" else "Urlaubsmodus aus",
            summary,
            src="sec.vacation",
            url="/",
            tag="vacation",
            force=True,
        )

    def _summary(self, since: int) -> str:
        data = self.store.data
        days = max(1, round((now_ms() - since) / 86_400_000))
        rings = sum(
            1
            for d in (data.get("doorbells") or {}).values()
            for t in (d or {}).get("rings") or []
            if t >= since
        )
        post = sum(1 for t in (data.get("mailbox") or {}).get("ins") or [] if t >= since)
        reason = (data.get("alarm") or {}).get("reason") or {}
        parts = [f"{days} {'Tag' if days == 1 else 'Tage'} weg"]
        if reason.get("t", 0) >= since:
            parts.append(f"Alarm ausgelöst ({reason.get('name')})")
        if rings:
            parts.append(f"{rings}× geklingelt")
        if post:
            parts.append(f"{post}× Post")
        if n := int(self.data.get("suppressed") or 0):
            parts.append(f"{n} {'Mitteilung' if n == 1 else 'Mitteilungen'} zurückgehalten")
        parts.append("Heizung und Warmwasser wieder normal")
        return " · ".join(parts)

    @callback
    def _save(self) -> None:
        self.store.save(SIGNAL_VACATION, SIGNAL_DATA)

    def info(self) -> dict[str, Any]:
        """Return the state for the dashboard."""
        return {
            "on": self.on,
            "since": self.data.get("since"),
            "settings": self.settings(),
            "preheated": bool(self.data.get("preheated")),
            "suppressed": int(self.data.get("suppressed") or 0),
            "lowered": len(self.data.get("prev") or {}),
        }
