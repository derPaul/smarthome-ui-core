"""Forecasts for automations: battery empty / charging again, best solar window, hot water.

The dashboard showed these on its Haustechnik page; here they become sensors (device
"Haustechnik") that automations can use. The sensors come from ``technik/settings``.

* **battery** – the battery power is sampled every minute and kept as mean per hour (7 days).
  From the typical course per hour (starting from the current power) Core rolls the state of
  charge forward in 15 minute steps for 36 hours: when it reaches the reserve ("empty") and from
  when it charges again. Needs 3 days of data for 20 of the 24 hours,
* **solar window** – the solar forecast of the energy dashboard (Forecast.Solar, Solcast …) minus
  what the house usually uses: the 3 hours of today with the most surplus (at least 2.5 kWh),
* **hot water** – the temperature at which the heat pump starts heating hot water, learned from
  the lowest values before each heating (rise of 3 K within 3 hours; otherwise target − 5 K), and
  the time it gets there at the current cooling rate.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from statistics import median
from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import (
    CALLBACK_TYPE,
    Event,
    EventStateChangedData,
    HomeAssistant,
    callback,
)
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import SIGNAL_FORECAST
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

STEP_MS = 15 * 60_000
AHEAD_MS = 36 * 3_600_000
HOURS_KEEP = 7 * 24
SOLAR_MIN_WH = 2500
DHW_RISE = 3.0
DHW_RISE_MS = 3 * 3_600_000
DHW_KEEP = 14
INVALID = ("unknown", "unavailable", "")


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(ms: int | None) -> datetime | None:
    return dt_util.utc_from_timestamp(ms / 1000) if ms else None


class Forecasts:
    """Battery, solar window and hot water forecasts."""

    def __init__(self, hass: HomeAssistant, store: CoreStore, technik: Any) -> None:
        """Initialize (the sensors come from the house technology settings)."""
        self.hass = hass
        self.store = store
        self.technik = technik
        self._unsub: list[CALLBACK_TYPE] = []
        self._listener: CALLBACK_TYPE | None = None
        self._hour: str | None = None
        self._acc: list[float] = []
        self.battery: dict[str, Any] = {}
        self.solar: dict[str, Any] = {}
        self.dhw: dict[str, Any] = {}
        self._dhw_low: tuple[float, int] | None = None
        self._dhw_peak: tuple[float, int] | None = None
        self._dhw_heating = False

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored samples."""
        return self.store.data["forecast"]

    def _s(self) -> dict[str, Any]:
        return self.technik.settings()

    def _value(self, entity_id: str | None) -> float | None:
        state = self.hass.states.get(entity_id or "")
        return _num(state.state) if state is not None and state.state not in INVALID else None

    def _watts(self, entity_id: str | None, invert: bool = False) -> float | None:
        state = self.hass.states.get(entity_id or "")
        value = _num(state.state) if state is not None else None
        if value is None:
            return None
        if state.attributes.get("unit_of_measurement") == "kW":
            value *= 1000
        return -value if invert else value

    # ---------------------------------------------------------------- setup

    @callback
    def async_setup(self) -> None:
        """Start sampling and forecasting."""
        self._unsub.append(async_track_time_interval(self.hass, self._minute, timedelta(minutes=1)))
        self._unsub.append(
            async_track_time_interval(self.hass, self._async_solar, timedelta(minutes=30))
        )
        self.listen()
        self.hass.async_create_task(self._async_solar())

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._listener:
            self._listener()
            self._listener = None

    @callback
    def listen(self) -> None:
        """Follow the hot water temperature (again after new settings)."""
        if self._listener:
            self._listener()
            self._listener = None
        dhw = self._s().get("dhw")
        if not dhw:
            return

        @callback
        def relevant(data: EventStateChangedData) -> bool:
            return data["entity_id"] == dhw

        @callback
        def changed(event: Event[EventStateChangedData]) -> None:
            new = event.data["new_state"]
            if new is not None and (value := _num(new.state)) is not None:
                self.dhw_sample(value)

        self._listener = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, changed, event_filter=relevant
        )
        if (value := self._value(dhw)) is not None:
            self.dhw_sample(value)

    @callback
    def _minute(self, _now: Any = None) -> None:
        self.battery_sample()
        self.update()

    @callback
    def update(self) -> None:
        """Recompute battery and hot water; tell the sensors if something changed."""
        battery, dhw = self.battery_forecast(), self.dhw_forecast()
        if battery != self.battery or dhw != self.dhw:
            self.battery, self.dhw = battery, dhw
            async_dispatcher_send(self.hass, SIGNAL_FORECAST)

    # ---------------------------------------------------------------- battery

    def _bat(self) -> dict[str, Any]:
        return self._s().get("battery") or {}

    @callback
    def battery_sample(self) -> None:
        """Add the current battery power to the mean of this hour."""
        bat = self._bat()
        watts = self._watts(bat.get("power"), bool(bat.get("invert")))
        hour = dt_util.now().strftime("%Y-%m-%dT%H")
        if hour != self._hour:
            if self._hour is not None and self._acc:
                hours = self.data.setdefault("battery_hours", {})
                hours[self._hour] = round(sum(self._acc) / len(self._acc), 1)
                for old in sorted(hours)[:-HOURS_KEEP]:
                    hours.pop(old)
                self.store.save()
            self._hour, self._acc = hour, []
        if watts is not None:
            self._acc.append(watts)

    def battery_profile(self) -> list[float | None] | None:
        """Return the mean battery power per hour of day (W, + charging), None if too few."""
        sums, counts = [0.0] * 24, [0] * 24
        for key, value in (self.data.get("battery_hours") or {}).items():
            hour = int(key[-2:])
            sums[hour] += value
            counts[hour] += 1
        if sum(1 for n in counts if n >= 3) < 20:
            return None
        return [s / n if n else None for s, n in zip(sums, counts, strict=True)]

    def battery_forecast(self, now: int | None = None) -> dict[str, Any]:
        """Return when the battery is empty / charges again (ms) and its lowest charge."""
        bat = self._bat()
        soc = self._value(bat.get("soc"))
        cap = _num(bat.get("capacity"))
        profile = self.battery_profile()
        if soc is None or not cap or profile is None:
            return {}
        now = now or now_ms()
        current = self._watts(bat.get("power"), bool(bat.get("invert")))
        max_d = self._watts(bat.get("max_discharge"))
        full = cap * 1000
        reserve = (_num(bat.get("reserve")) or 0) / 100 * full
        energy = soc / 100 * full
        empty = charge = None
        low = soc
        t = now
        while t < now + AHEAD_MS:
            hour = dt_util.as_local(dt_util.utc_from_timestamp(t / 1000)).hour
            power = profile[hour] or 0.0
            first = 1 - (t - now) / 3_600_000 if t - now < 3_600_000 else 0
            if current is not None:
                power = power * (1 - first) + current * first
            if max_d and power < -max_d:
                power = -max_d
            if power > 50 and t - now > 3_600_000 and charge is None and energy < full * 0.98:
                charge = t
            energy = min(full, max(reserve, energy + power * STEP_MS / 3_600_000))
            low = min(low, energy / full * 100)
            if energy <= reserve + 1 and empty is None:
                empty = t + STEP_MS
                break
            if charge is not None and t - charge > 2 * 3_600_000:
                break
            t += STEP_MS
        return {"empty": empty, "charge": charge, "min": round(low, 1)}

    # ---------------------------------------------------------------- solar window

    async def _async_solar(self, _now: Any = None) -> None:
        try:
            hours = await self._async_solar_hours()
        except Exception as err:  # noqa: BLE001 - the energy dashboard is optional
            _LOGGER.debug("No solar forecast: %s", err)
            hours = {}
        solar = self.solar_window(hours)
        if solar != self.solar:
            self.solar = solar
            async_dispatcher_send(self.hass, SIGNAL_FORECAST)

    async def _async_solar_hours(self) -> dict[int, float]:
        """Return the forecast Wh per hour (ms) of all solar forecasts of the energy dashboard."""
        from homeassistant.components.energy.data import async_get_manager
        from homeassistant.components.energy.websocket_api import async_get_energy_platforms

        manager = await async_get_manager(self.hass)
        if manager.data is None:
            return {}
        entries = {
            entry
            for source in manager.data["energy_sources"]
            if source["type"] == "solar"
            for entry in source.get("config_entry_solar_forecast") or []
        }
        platforms = await async_get_energy_platforms(self.hass)
        hours: dict[int, float] = {}
        for entry_id in entries:
            entry = self.hass.config_entries.async_get_entry(entry_id)
            if entry is None or entry.domain not in platforms:
                continue
            forecast = await platforms[entry.domain](self.hass, entry_id)
            for when, wh in ((forecast or {}).get("wh_hours") or {}).items():
                parsed = dt_util.parse_datetime(when)
                if parsed is not None:
                    key = int(parsed.timestamp() * 1000)
                    hours[key] = hours.get(key, 0) + (_num(wh) or 0)
        return hours

    def solar_window(self, hours: dict[int, float], now: int | None = None) -> dict[str, Any]:
        """Return the best 3 hours of today (start, end ms, surplus kWh) or {}."""
        now = now or now_ms()
        house = self.technik.house_power()
        base = max(300.0, min(900.0, house if house is not None else 500.0))
        end_of_day = dt_util.as_local(dt_util.utc_from_timestamp(now / 1000)).replace(
            hour=23, minute=59, second=0, microsecond=0
        )
        last = int(end_of_day.timestamp() * 1000)
        slots = sorted(
            (t, max(0.0, wh - base)) for t, wh in hours.items() if now - 1_800_000 <= t <= last
        )
        best: tuple[int, float] | None = None
        for i in range(len(slots) - 2):
            if slots[i + 2][0] - slots[i][0] > 2 * 3_600_000 + 60_000:
                continue
            total = slots[i][1] + slots[i + 1][1] + slots[i + 2][1]
            if best is None or total > best[1]:
                best = (slots[i][0], total)
        if best is None or best[1] < SOLAR_MIN_WH:
            return {}
        return {"start": best[0], "end": best[0] + 3 * 3_600_000, "kwh": round(best[1] / 1000, 1)}

    # ---------------------------------------------------------------- hot water

    @callback
    def dhw_sample(self, value: float, t: int | None = None) -> None:
        """Follow the hot water temperature: find the start of each heating."""
        t = t or now_ms()
        if not 5 < value < 95:
            return
        low, peak = self._dhw_low, self._dhw_peak
        if self._dhw_heating:
            if peak is None or value >= peak[0]:
                self._dhw_peak = (value, t)
            elif peak[0] - value >= 0.5:  # cools down again
                self._dhw_heating = False
                self._dhw_low = (value, t)
            return
        if low is None or value <= low[0] or t - low[1] > DHW_RISE_MS:
            self._dhw_low = (value, t)
            if self._dhw_peak is None:
                self._dhw_peak = (value, t)
            return
        if value - low[0] >= DHW_RISE:
            starts = self.data.setdefault("dhw_starts", [])
            starts.append(round(low[0], 1))
            del starts[:-DHW_KEEP]
            self.store.save()
            self._dhw_heating = True
            self._dhw_peak = (value, t)

    def dhw_forecast(self, now: int | None = None) -> dict[str, Any]:
        """Return the start temperature and when it is reached (ms)."""
        s = self._s()
        ist = self._value(s.get("dhw"))
        if ist is None:
            return {}
        now = now or now_ms()
        starts = self.data.get("dhw_starts") or []
        soll = self._value(s.get("dhw_target"))
        learned = len(starts) >= 2
        at = median(starts) if learned else (soll - 5 if soll is not None else None)
        if at is None:
            return {}
        rate = eta = None
        peak = self._dhw_peak
        if not self._dhw_heating and peak and now - peak[1] >= 45 * 60_000 and peak[0] > ist:
            rate = (peak[0] - ist) / ((now - peak[1]) / 3_600_000)
            if ist > at and rate > 0.05:
                eta = now + int((ist - at) / rate * 3_600_000)
        return {
            "at": round(at, 1),
            "learned": learned,
            "rate": round(rate, 2) if rate else None,
            "eta": eta,
            "heating": self._dhw_heating,
            "due": not self._dhw_heating and ist <= at + 0.3,
        }

    # ---------------------------------------------------------------- dashboard

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        return {"battery": self.battery, "solar": self.solar, "dhw": self.dhw}
