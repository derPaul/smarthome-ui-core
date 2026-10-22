"""Watchers: low batteries and frost – without a dashboard (plants: see plants.py).

They become tasks (to-do list "Haushalt" or "Garten", due notification at the task hour, check off
= done until the next time) and – for frost – a notification in the late afternoon before:

* battery: ``sensor`` with device class battery below ``BATTERY_LOW`` % (or a battery
  ``binary_sensor`` that is on) → "Batterie wechseln: <device>" (source ``hh.battery``),
* frost: the hourly forecast of the first weather entity goes down to ``FROST_C`` °C within the
  next 18 hours → "Pflanzen vor Frost schützen" (``gd.frost``).

The key of each task contains the day the problem started, so checking it off hides it until the
problem comes again.
"""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SIGNAL_TASKS, WATCH_INTERVAL
from .store import CoreStore

_LOGGER = logging.getLogger(__name__)

BATTERY_LOW = 15
FROST_C = 1.0
FROST_HOURS = 18
FROST_NOTIFY_HOUR = 17


def _ms(state: State) -> int:
    return int(state.last_changed.timestamp() * 1000)


def _day(state: State) -> str:
    return dt_util.as_local(state.last_changed).date().isoformat()


class Watchers:
    """Low batteries and frost as tasks."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize the watchers."""
        self.hass = hass
        self.store = store
        self.frost: dict[str, Any] | None = None  # {"t": °C, "at": ms}
        self.hourly: list[dict[str, Any]] = []  # hourly forecast (rain for the plants)
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_setup(self) -> None:
        """Read the forecast now and every half hour."""
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_forecast, WATCH_INTERVAL)
        )
        self.hass.async_create_task(self._async_forecast())

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    # ---------------------------------------------------------------- batteries

    def _name(self, entity_id: str, state: State) -> str:
        entry = er.async_get(self.hass).async_get(entity_id)
        if entry and entry.device_id:
            device = dr.async_get(self.hass).async_get(entry.device_id)
            if device:
                return device.name_by_user or device.name or entity_id
        return str(state.attributes.get("friendly_name") or entity_id)

    def low_batteries(self) -> list[tuple[str, State]]:
        """Return the battery entities that are low (phones and hidden entities left out)."""
        registry = er.async_get(self.hass)
        out = []
        for state in [
            *self.hass.states.async_all("sensor"),
            *self.hass.states.async_all("binary_sensor"),
        ]:
            if state.attributes.get("device_class") != "battery":
                continue
            entry = registry.async_get(state.entity_id)
            if entry and (entry.hidden_by or entry.disabled_by or entry.platform == "mobile_app"):
                continue
            if state.domain == "binary_sensor":
                low = state.state == "on"
            else:
                try:
                    low = float(state.state) < BATTERY_LOW
                except ValueError:
                    low = False
            if low:
                out.append((state.entity_id, state))
        return out

    # ---------------------------------------------------------------- frost

    async def _async_forecast(self, _now: Any = None) -> None:
        weather = next(iter(self.hass.states.async_all("weather")), None)
        if weather is None:
            return
        try:
            response = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"type": "hourly"},
                target={"entity_id": weather.entity_id},
                blocking=True,
                return_response=True,
            )
        except (HomeAssistantError, ValueError) as err:
            _LOGGER.debug("No hourly forecast: %s", err)
            return
        now = dt_util.utcnow()
        coldest = None
        self.hourly = list((response or {}).get(weather.entity_id, {}).get("forecast", []))
        for item in self.hourly:
            when = dt_util.parse_datetime(str(item.get("datetime", "")))
            temp = item.get("temperature")
            if (
                when is None
                or temp is None
                or not now <= when <= now + timedelta(hours=FROST_HOURS)
            ):
                continue
            if coldest is None or float(temp) < coldest["t"]:
                coldest = {"t": float(temp), "at": int(when.timestamp() * 1000)}
        frost = coldest if coldest and coldest["t"] <= FROST_C else None
        if frost != self.frost:
            self.frost = frost
            async_dispatcher_send(self.hass, SIGNAL_TASKS)

    def frost_key(self) -> str | None:
        """Return the done key of the current frost warning (one per night)."""
        if not self.frost:
            return None
        night = dt_util.as_local(dt_util.utc_from_timestamp(self.frost["at"] / 1000))
        if night.hour < 12:
            night -= timedelta(days=1)
        return f"frost:{night.date().isoformat()}"

    # ---------------------------------------------------------------- tasks and rules

    def tasks(self) -> list[dict[str, Any]]:
        """Return the watchers as tasks."""
        out = []
        for entity_id, state in self.low_batteries():
            name = self._name(entity_id, state)
            level = "" if state.domain == "binary_sensor" else f" ({round(float(state.state))} %)"
            out.append(
                {
                    "key": f"battery:{entity_id}|{_day(state)}",
                    "name": f"Batterie wechseln: {name}{level}",
                    "src": "hh.battery",
                    "area": None,
                    "icon": "mdi:battery-alert-variant-outline",
                    "interval": None,
                    "last": None,
                    "due": _ms(state),
                }
            )
        if key := self.frost_key():
            out.append(
                {
                    "key": key,
                    "name": f"Pflanzen vor Frost schützen ({round(self.frost['t'])} °C erwartet)",
                    "src": "gd.frost",
                    "area": None,
                    "icon": "mdi:snowflake-alert",
                    "interval": None,
                    "last": None,
                    "due": int(dt_util.utcnow().timestamp() * 1000),
                }
            )
        return out

    def notify_tasks(self) -> list[dict[str, Any]]:
        """Return the tasks announced at the task hour (frost has its own evening notification)."""
        return [t for t in self.tasks() if t["src"] != "gd.frost"]

    def rules(self) -> list[dict[str, Any]]:
        """Return the frost notification (late afternoon before the frost)."""
        cfg = (self.store.data["notify"].get("sources") or {}).get("gd.frost") or {}
        key = self.frost_key()
        if not cfg.get("push") or key is None or self.frost is None:
            return []
        local = dt_util.now()
        at = local.replace(hour=FROST_NOTIFY_HOUR, minute=0, second=0, microsecond=0)
        frost_at = dt_util.as_local(dt_util.utc_from_timestamp(self.frost["at"] / 1000))
        when = "heute Nacht" if frost_at.date() == local.date() or frost_at.hour < 12 else "morgen"
        return [
            {
                "id": key.replace(":", "|"),
                "type": "at",
                "at": int(min(at, max(local, frost_at - timedelta(hours=8))).timestamp() * 1000),
                "skip_if_done": key,
                "src": "gd.frost",
                "title": "Frostwarnung",
                "urgent": bool(cfg.get("urgent")),
                "url": cfg.get("url") or "/",
                "message": f"Frost {when} ({round(self.frost['t'])} °C) – Pflanzen schützen",
            }
        ]
