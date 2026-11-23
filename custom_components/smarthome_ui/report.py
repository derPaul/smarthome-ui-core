"""Weekly report: on Sunday evening a summary of the last seven days, compared with the week before.

Content (whatever is available):

* electricity used in the house, from the grid and fed in – with costs from the prices of the energy
  dashboard –, solar yield with the best day, self-sufficiency,
* heating: what the heat pump used (consumer of the energy dashboard named heat pump, else the mean
  of its power sensor from Haustechnik),
* water from the water meters of the energy dashboard,
* weather: mean, lowest and highest outdoor temperature (sensor found by the catalog),
* events: doorbell rings, mail deliveries, alarm triggered, tasks done, mowed areas, vacuumed rooms,
* the tasks due next week.

Each value comes with the week before (``prev``). Sent as a push (source ``home.report``,
"Wochenbericht" in the dashboard's notifications, push on by default) every Sunday at 18:12 and kept
as ``report`` for the dashboard (shown until Monday evening).

Heat pump: with the meters of heat produced and electricity used (``hp_meters`` from Haustechnik)
the report shows the coefficient of performance (Arbeitszahl) of the week. Each Sunday Core keeps it
with the mean outdoor temperature (``hp_weeks``, one year); if it is clearly worse than in weeks
with similar weather, the report says so.

Heating curve: every 30 minutes, while it is cooler than 15 °C outside, Core looks at the
thermostats in heat mode (rooms with a target of 17 °C or more): how far each room is from its
target and whether the thermostat is heating (``hc_samples``, two weeks, not during holiday mode).
If rooms stay too cold while their thermostats heat, the curve is too low; if they reach their
target with the thermostats hardly ever heating (or are clearly too warm), the flow temperature is
higher than needed, which costs efficiency. Comparing cold (below 3 °C) with mild days (8 °C and
above) tells the slope from the base point. The report adds one line with the suggestion
(``heating.curve``).

Monthly balance: on the 1st of each month at 09:12 the same numbers for the month before, compared
with the month before that and the same month a year ago (``report_month``, title "Monatsbilanz").
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
import re
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_change
from homeassistant.util import dt as dt_util

from .const import SIGNAL_DATA
from .notify import NotifyEngine
from .store import CoreStore, now_ms
from .tasks import task_states

_LOGGER = logging.getLogger(__name__)

SRC = "home.report"
WEEKDAY = 6  # Sunday
RUN_AT = (18, 12)
MONTH_AT = (9, 12)
MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]
HP_WEEKS = 53
COP_WORSE = 0.4  # this much below weeks with similar weather → hint
SIMILAR_DEG = 2.0
DAY_MS = 86_400_000
CURVE_EVERY = 30  # minutes between heating curve samples
CURVE_KEEP = 14 * DAY_MS
CURVE_MAX_OUT = 15.0  # warmer outside: no heating season
CURVE_MIN_TARGET = 17.0  # rooms kept cooler (bedrooms, eco) say little about the curve
CURVE_COLD = 0.5  # this much below target while heating → too cold
CURVE_WARM = 0.8  # this much above target → too warm
CURVE_MIN_SAMPLES = 48  # one day of samples
WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_HEATPUMP = re.compile(r"(wärmepumpe|waermepumpe|heat ?pump|\bwp\b|lwz|luft.?wasser)", re.I)


def _eur(value: float) -> str:
    return f"{value:.2f} €".replace(".", ",")


def _kwh(value: float) -> str:
    return f"{value:.0f} kWh" if value >= 10 else f"{value:.1f} kWh".replace(".", ",")


def _deg(value: float) -> str:
    return f"{value:.1f}°".replace(".", ",").replace("-", "−")


def _liters(value: float) -> str:
    return f"{value:,.0f} L".replace(",", ".")


def _delta(cur: float | None, prev: float | None, to: str = "zur Vorwoche") -> str:
    """Return the change against the period before ("+12 % zur Vorwoche"); empty if unknown."""
    if cur is None or not prev or prev < 0.5:
        return ""
    pct = round((cur - prev) / prev * 100)
    if pct == 0:
        return f"gleich {to.replace('zur ', 'wie ').replace('zum ', 'wie ')}"
    return f"{'+' if pct > 0 else '−'}{abs(pct)} % {to}"


def _cop(value: float) -> str:
    return f"{value:.1f}".replace(".", ",")


class WeeklyReport:
    """Collect the week and send it."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        engine: NotifyEngine,
        energy: Callable[[], dict[str, Any]],
        tasks: Callable[[], list[dict[str, Any]]],
        context: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self.engine = engine
        self.energy = energy
        self.tasks = tasks
        # outdoor temperature sensor (catalog) and heat pump power sensor (Haustechnik)
        self.context = context or dict
        self._unsub: list[CALLBACK_TYPE] = []

    @callback
    def async_setup(self) -> None:
        """Run every Sunday evening."""
        self._unsub.append(
            async_track_time_change(
                self.hass, self._tick, hour=RUN_AT[0], minute=RUN_AT[1], second=0
            )
        )
        self._unsub.append(
            async_track_time_change(
                self.hass, self._tick_month, hour=MONTH_AT[0], minute=MONTH_AT[1], second=0
            )
        )
        self._unsub.append(
            async_track_time_change(
                self.hass, self._tick_curve, minute=list(range(4, 60, CURVE_EVERY)), second=0
            )
        )

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _tick(self, now: datetime) -> None:
        if dt_util.as_local(now).weekday() == WEEKDAY:
            self.hass.async_create_task(self.async_run(remember=True))

    @callback
    def _tick_month(self, now: datetime) -> None:
        if dt_util.as_local(now).day == 1:
            self.hass.async_create_task(self.async_run_month())

    @callback
    def _tick_curve(self, now: datetime) -> None:
        self.sample_curve()

    # ---------------------------------------------------------------- heating curve

    def _outdoor_now(self) -> float | None:
        try:
            ent = (self.context().get("outdoor") or {}).get("temperature")
        except Exception:  # noqa: BLE001 - catalog not available
            return None
        st = self.hass.states.get(ent) if ent else None
        try:
            return float(st.state) if st else None
        except (TypeError, ValueError):
            return None

    @callback
    def sample_curve(self) -> dict[str, Any] | None:
        """Look at the thermostats in heat mode and keep one sample (None: nothing to keep)."""
        try:
            if self.context().get("vacation"):
                return None
        except Exception:  # noqa: BLE001 - context not available
            pass
        out = self._outdoor_now()
        if out is None or out >= CURVE_MAX_OUT:
            return None
        rooms = cold = warm = heating = known = 0
        deltas: list[float] = []
        for st in self.hass.states.async_all("climate"):
            a = st.attributes
            if st.state != "heat":
                continue
            try:
                cur, target = float(a["current_temperature"]), float(a["temperature"])
            except (KeyError, TypeError, ValueError):
                continue
            if target < CURVE_MIN_TARGET:
                continue
            rooms += 1
            d = cur - target
            deltas.append(d)
            action = a.get("hvac_action")
            if action in ("heating", "idle", "off"):
                known += 1
                heating += action == "heating"
            if d <= -CURVE_COLD and action != "idle":
                cold += 1
            elif d >= CURVE_WARM:
                warm += 1
        if not rooms:
            return None
        sample = {
            "t": now_ms(),
            "out": round(out, 1),
            "rooms": rooms,
            "cold": cold,
            "warm": warm,
            "d": round(sum(deltas) / rooms, 2),
            "duty": round(heating / known, 2) if known else None,
        }
        keep = [
            x for x in self.store.data.get("hc_samples") or [] if x["t"] > now_ms() - CURVE_KEEP
        ]
        self.store.data["hc_samples"] = [*keep, sample]
        self.store.save()
        return sample

    def curve(self, start: datetime, end: datetime) -> dict[str, Any] | None:
        """Return what the samples of the period say about the heating curve."""
        since, until = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        samples = [x for x in self.store.data.get("hc_samples") or [] if since <= x["t"] < until]
        if len(samples) < CURVE_MIN_SAMPLES:
            return None

        def part(xs: list[dict[str, Any]]) -> dict[str, Any] | None:
            n = sum(x["rooms"] for x in xs)
            if len(xs) < 12 or not n:
                return None
            duties = [x["duty"] for x in xs if x.get("duty") is not None]
            return {
                "cold": round(sum(x["cold"] for x in xs) / n, 2),
                "warm": round(sum(x["warm"] for x in xs) / n, 2),
                "d": round(sum(x["d"] * x["rooms"] for x in xs) / n, 2),
                "duty": round(sum(duties) / len(duties), 2) if duties else None,
                "n": len(xs),
            }

        res: dict[str, Any] = {"all": part(samples)}
        res["cold_days"] = part([x for x in samples if x["out"] < 3])
        res["mild_days"] = part([x for x in samples if x["out"] >= 8])
        res["rooms"] = max(x["rooms"] for x in samples)

        def verdict(p: dict[str, Any] | None) -> str | None:
            if not p:
                return None
            if p["cold"] >= 0.2:
                return "raise"
            low_duty = p["duty"] is not None and p["duty"] < 0.25 and p["d"] >= -0.2
            if p["cold"] < 0.05 and (p["warm"] >= 0.3 or low_duty):
                return "lower"
            return None

        advice = verdict(res["all"])
        cold, mild = verdict(res["cold_days"]), verdict(res["mild_days"])
        part_name = None
        if cold and cold != mild and res["mild_days"]:
            advice, part_name = cold, "slope"
        elif mild and mild != cold and res["cold_days"]:
            advice, part_name = mild, "base"
        elif advice:
            part_name = "slope" if res["cold_days"] and not res["mild_days"] else "base"
        res["advice"] = advice
        res["part"] = part_name if advice else None
        return res

    # ---------------------------------------------------------------- collecting

    async def _stat(
        self,
        statistic_id: str | None,
        start: datetime,
        end: datetime,
        types: set[str],
        units: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return the long-term statistics of a sensor between start and end (empty if unknown)."""
        if not statistic_id:
            return {}
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
                statistic_during_period,
            )

            res = await get_instance(self.hass).async_add_executor_job(
                statistic_during_period,
                self.hass,
                start,
                end,
                statistic_id,
                types,
                units or {},
            )
        except Exception:  # noqa: BLE001 - recorder not available or no statistics
            return {}
        return res or {}

    async def _change(self, statistic_id: str | None, start: datetime, end: datetime) -> float:
        """Return how much a meter counted between start and end (kWh, 0 if unknown)."""
        res = await self._stat(statistic_id, start, end, {"change"}, {"energy": "kWh"})
        return float(res.get("change") or 0)

    async def _volume(self, statistic_id: str | None, start: datetime, end: datetime) -> float:
        """Return how much a water meter counted between start and end (L, 0 if unknown)."""
        res = await self._stat(statistic_id, start, end, {"change"}, {"volume": "L"})
        return float(res.get("change") or 0)

    async def _daily(self, ids: list[str], start: datetime, end: datetime) -> list[float]:
        """Return the daily sums of meters (kWh) for the days from start to end."""
        n = max(1, round((end - start).total_seconds() / 86400))
        out = [0.0] * n
        if not ids:
            return out
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415
            from homeassistant.components.recorder.statistics import (  # noqa: PLC0415
                statistics_during_period,
            )

            res = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                set(ids),
                "day",
                {"energy": "kWh"},
                {"change"},
            )
        except Exception:  # noqa: BLE001 - recorder not available or no statistics
            return out
        for rows in (res or {}).values():
            for row in rows:
                t = row.get("start")
                t = t if isinstance(t, (int, float)) else getattr(t, "timestamp", lambda: 0)()
                i = int((float(t) - start.timestamp()) // 86400)
                if 0 <= i < n:
                    out[i] += float(row.get("change") or 0)
        return out

    def _price(self, kind: str, default: float) -> float:
        prefs = self.energy()
        if (ent := prefs.get(f"price_{kind}_entity")) and (st := self.hass.states.get(ent)):
            try:
                return float(st.state)
            except ValueError:
                pass
        value = prefs.get(f"price_{kind}")
        return float(value) if value is not None else default

    def _heatpump(self) -> tuple[str | None, str | None]:
        """Return the heat pump's energy meter (energy dashboard consumer) or power sensor."""
        consumers = [c for c in self.energy().get("consumers") or [] if c.get("entity")]
        # first the one named "Wärmepumpe" (its parts – hot water, heating – carry the same model
        # name in their entity ids), then anything that looks like a heat pump
        for c in consumers:
            if re.search(r"(wärmepumpe|waermepumpe|heat ?pump)", c.get("name") or "", re.I):
                return c["entity"], None
        for c in consumers:
            st = self.hass.states.get(c["entity"])
            friendly = st.attributes.get("friendly_name", "") if st else ""
            if _HEATPUMP.search(f"{c.get('name') or ''} {c['entity']} {friendly}"):
                return c["entity"], None
        try:
            return None, (self.context().get("technik") or {}).get("hp_power")
        except Exception:  # noqa: BLE001 - Haustechnik not available
            return None, None

    async def _energy(self, start: datetime, end: datetime) -> dict[str, Any] | None:
        prefs = self.energy()

        async def total(kind: str) -> float:
            return sum([await self._change(s, start, end) for s in prefs.get(kind) or []])

        imp, exp, pv = await total("import"), await total("export"), await total("pv")
        charge, discharge = await total("battery_charge"), await total("battery_discharge")
        if not (imp or exp or pv):
            return None
        used = max(0.0, imp - exp + pv + discharge - charge)
        p_imp, p_exp = self._price("import", 0.35), self._price("export", 0.08)
        return {
            "used": round(used, 1),
            "import": round(imp, 1),
            "export": round(exp, 1),
            "pv": round(pv, 1),
            "paid": round(imp * p_imp, 2),
            "earned": round(exp * p_exp, 2),
            "saved": round(max(0.0, pv - exp) * p_imp, 2),
            "autarky": round(100 * (1 - imp / used)) if used else None,
        }

    async def _heat(self, start: datetime, end: datetime) -> float | None:
        meter, power = self._heatpump()
        if meter:
            return round(await self._change(meter, start, end), 1)
        if power:
            res = await self._stat(power, start, end, {"mean"}, {"power": "W"})
            if res.get("mean") is not None:
                hours = (end - start).total_seconds() / 3600
                return round(float(res["mean"]) * hours / 1000, 1)
        return None

    def _hp_meters(self) -> dict[str, str]:
        try:
            meters = (self.context().get("technik") or {}).get("hp_meters") or {}
        except Exception:  # noqa: BLE001 - Haustechnik not available
            return {}
        return {k: v for k, v in meters.items() if v}

    async def _cop(
        self, start: datetime, end: datetime, used: float | None
    ) -> dict[str, Any] | None:
        """Return heat produced (kWh) and the coefficient of performance of the period."""
        m = self._hp_meters()
        heat = sum([await self._change(m.get(k), start, end) for k in ("heat_out", "dhw_out")])
        el = sum([await self._change(m.get(k), start, end) for k in ("heat_in", "dhw_in")])
        el = el or used or 0
        if heat <= 0 or el <= 0:
            return None
        return {"heat": round(heat, 1), "cop": round(heat / el, 2)}

    async def _water(self, start: datetime, end: datetime) -> float | None:
        ids = self.energy().get("water") or []
        if not ids:
            return None
        return round(sum([await self._volume(s, start, end) for s in ids]), 0)

    async def _weather(self, start: datetime, end: datetime) -> dict[str, Any] | None:
        try:
            ent = (self.context().get("outdoor") or {}).get("temperature")
        except Exception:  # noqa: BLE001 - catalog not available
            ent = None
        res = await self._stat(ent, start, end, {"mean", "min", "max"}, {"temperature": "°C"})
        if res.get("mean") is None:
            return None
        return {
            "mean": round(float(res["mean"]), 1),
            "min": round(float(res["min"]), 1) if res.get("min") is not None else None,
            "max": round(float(res["max"]), 1) if res.get("max") is not None else None,
        }

    def _events(self, since: int, until: int) -> dict[str, Any]:
        data = self.store.data
        rings = sum(
            1
            for d in (data.get("doorbells") or {}).values()
            for t in (d or {}).get("rings") or []
            if since <= t < until
        )
        post = sum(1 for t in (data.get("mailbox") or {}).get("ins") or [] if since <= t < until)
        reason = (data.get("alarm") or {}).get("reason") or {}
        alarm = reason.get("name") if since <= (reason.get("t") or 0) < until else None
        done = sum(
            1 for e in data["data"].get("done_log") or [] if since <= (e or {}).get("t", 0) < until
        )
        mowed = [
            a
            for a, r in (data["data"].get("mow_log") or {}).items()
            if since <= (r or {}).get("last", 0) < until
        ]
        cleaned = (data["data"].get("household") or {}).get("cleaned") or {}
        vacuumed = [
            a
            for a, r in cleaned.items()
            if since <= max((r or {}).get("r", 0), (r or {}).get("w", 0)) < until
        ]
        return {
            "rings": rings,
            "post": post,
            "alarm": alarm,
            "done": done,
            "mowed": len(mowed),
            "vacuumed": len(vacuumed),
        }

    async def collect(self, end: datetime | None = None) -> dict[str, Any]:
        """Return the numbers of the seven days before ``end`` and of the week before."""
        end = end or dt_util.now()
        start = end - timedelta(days=7)
        out = await self.collect_period(start, end, start - timedelta(days=7), start)
        if h := out.get("heating"):
            self._cop_hint(h, start, out.get("weather"))
        if curve := self.curve(start, end):
            out.setdefault("heating", {})["curve"] = curve
        week_end = now_ms() + 7 * DAY_MS
        due = [
            t
            for t in task_states(self.tasks(), self.store.done)
            if t.due is not None and t.due <= week_end
        ]
        out["due"] = len(due)
        out["due_names"] = [t.name for t in sorted(due, key=lambda t: t.due or 0)[:3]]
        return out

    async def collect_period(
        self,
        start: datetime,
        end: datetime,
        p_start: datetime,
        p_end: datetime,
        y_start: datetime | None = None,
        y_end: datetime | None = None,
    ) -> dict[str, Any]:
        """Return the numbers of a period, of the one before (prev) and a year before (year)."""
        out: dict[str, Any] = {}
        year = y_start is not None and y_end is not None
        if energy := await self._energy(start, end):
            energy["prev"] = await self._energy(p_start, p_end)
            if year:
                energy["year"] = await self._energy(y_start, y_end)
            # best solar day (from the daily sums of the solar meters)
            days = await self._daily(self.energy().get("pv") or [], start, end)
            if days and max(days) > 0:
                i = days.index(max(days))
                day = start + timedelta(days=i)
                energy["best_day"] = {
                    "day": WEEKDAYS[day.weekday()] if len(days) <= 7 else f"{day.day}.{day.month}.",
                    "kwh": round(days[i], 1),
                }
            out["energy"] = energy
        heat = await self._heat(start, end)
        if heat is not None:
            h: dict[str, Any] = {"heatpump": heat, "prev": await self._heat(p_start, p_end)}
            if cop := await self._cop(start, end, heat):
                h.update(cop)
                h["cop_prev"] = (await self._cop(p_start, p_end, h["prev"]) or {}).get("cop")
            if year:
                h["year"] = await self._heat(y_start, y_end)
            out["heating"] = h
        water = await self._water(start, end)
        if water:
            out["water"] = {"liters": water, "prev": await self._water(p_start, p_end)}
            if year:
                out["water"]["year"] = await self._water(y_start, y_end)
        if weather := await self._weather(start, end):
            weather["prev"] = await self._weather(p_start, p_end)
            if year:
                weather["year"] = await self._weather(y_start, y_end)
            out["weather"] = weather
            if out.get("heating"):
                out["heating"]["outdoor"] = weather["mean"]
        since, until = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        ev = self._events(since, until)
        ev["prev"] = self._events(int(p_start.timestamp() * 1000), int(p_end.timestamp() * 1000))
        out["events"] = ev
        # older fields, kept for the dashboard
        out["mowed"] = ev["mowed"]
        out["vacuumed"] = ev["vacuumed"]
        return out

    # ---------------------------------------------------------------- heat pump history

    @property
    def hp_weeks(self) -> list[dict[str, Any]]:
        """Return the kept weeks (coefficient of performance with the mean outdoor temperature)."""
        return self.store.data.setdefault("hp_weeks", [])

    def _cop_hint(self, h: dict[str, Any], start: datetime, weather: dict[str, Any] | None) -> None:
        """Add the history and, if the week is clearly worse than similar ones, a warning."""
        week = int(start.timestamp() * 1000)
        others = [w for w in self.hp_weeks if abs(w.get("t", 0) - week) > 3 * DAY_MS]
        cur = [{"t": week, "cop": h["cop"], "out": h.get("outdoor")}] if h.get("cop") else []
        h["history"] = [*others[-11:], *cur]
        if not h.get("cop") or not weather:
            return
        similar = [
            w["cop"]
            for w in others
            if w.get("cop")
            and w.get("out") is not None
            and abs(w["out"] - weather["mean"]) <= SIMILAR_DEG
        ]
        if len(similar) >= 2:
            usual = sum(similar) / len(similar)
            if usual - h["cop"] >= COP_WORSE:
                h["warn"] = {"usual": round(usual, 2), "weeks": len(similar)}

    def _remember(self, data: dict[str, Any], start: datetime) -> None:
        h = data.get("heating") or {}
        if not h.get("cop"):
            return
        week = int(start.timestamp() * 1000)
        weeks = [w for w in self.hp_weeks if abs(w.get("t", 0) - week) > 3 * DAY_MS]
        weeks.append(
            {
                "t": week,
                "cop": h["cop"],
                "out": h.get("outdoor"),
                "heat": h.get("heat"),
                "el": h.get("heatpump"),
            }
        )
        self.store.data["hp_weeks"] = weeks[-HP_WEEKS:]

    @staticmethod
    def curve_text(c: dict[str, Any] | None) -> str:
        """Return the heating curve suggestion as one line ("" if the curve fits or is unknown)."""
        if not c or not c.get("advice"):
            return ""
        p = c.get("cold_days") if c.get("part") == "slope" else c.get("mild_days")
        p = p or c["all"]
        when = {"slope": "an kalten Tagen", "base": "an milden Tagen"}.get(c.get("part"), "")
        when = f" {when}" if when and p is not c["all"] else ""
        if c["advice"] == "raise":
            why = f"{round(p['cold'] * 100)} % der Zeit zu kalt, obwohl die Thermostate heizen"
            what = (
                "Steigung etwas erhöhen (z. B. +0,05)"
                if c.get("part") == "slope"
                else "Fußpunkt etwa 1 Grad höher"
            )
            return f"Heizkurve: Räume{when} {why} – {what}."
        if p["d"] >= CURVE_WARM / 2:
            why = f"im Schnitt {_deg(p['d']).rstrip('°')} Grad über Soll"
        else:
            duty = round((p.get("duty") or 0) * 100)
            why = f"warm genug, Thermostate heizen nur {duty} % der Zeit"
        what = (
            "Steigung etwas senken (z. B. −0,05)"
            if c.get("part") == "slope"
            else "Fußpunkt etwa 1 Grad tiefer"
        )
        return f"Heizkurve: Räume{when} {why} – {what}, das verbessert die Arbeitszahl."

    @staticmethod
    def text(data: dict[str, Any], month: bool = False) -> list[str]:
        """Return the report as short lines (month: compared with the month and year before)."""
        lines: list[str] = []
        to = "zum Vormonat" if month else "zur Vorwoche"

        def vs(cur: float | None, prev: dict[str, Any] | None, key: str) -> str:
            d = _delta(cur, (prev or {}).get(key), to)
            return f" ({d})" if d else ""

        def yr(cur: float | None, year: Any, key: str | None = None) -> str:
            ref = (year or {}).get(key) if key else year
            d = _delta(cur, ref, "zum Vorjahr") if month else ""
            return f" ({d})" if d else ""

        if e := data.get("energy"):
            prev = e.get("prev")
            used, grid, paid = _kwh(e["used"]), _kwh(e["import"]), _eur(e["paid"])
            change = vs(e["used"], prev, "used") + yr(e["used"], e.get("year"), "used")
            lines.append(f"Strom: {used} verbraucht{change}, {grid} aus dem Netz ({paid})")
            if e["pv"]:
                fed, earned = _kwh(e["export"]), _eur(e["earned"])
                change = vs(e["pv"], prev, "pv") + yr(e["pv"], e.get("year"), "pv")
                pv = f"Solar: {_kwh(e['pv'])}{change}, {fed} eingespeist (+{earned})"
                if e.get("autarky") is not None:
                    pv += f", Autarkie {e['autarky']} %"
                lines.append(pv)
                if best := e.get("best_day"):
                    lines.append(f"Bester Sonnentag: {best['day']} ({_kwh(best['kwh'])})")
        if (h := data.get("heating")) and h.get("heatpump") is not None:
            d = _delta(h["heatpump"], h.get("prev"), to)
            line = f"Heizung: Wärmepumpe {_kwh(h['heatpump'])} Strom" + (f" ({d})" if d else "")
            line += yr(h["heatpump"], h.get("year"))
            if h.get("cop"):
                line += f", {_kwh(h['heat'])} Wärme, Arbeitszahl {_cop(h['cop'])}"
                if h.get("cop_prev"):
                    line += f" ({'Vormonat' if month else 'Vorwoche'} {_cop(h['cop_prev'])})"
            lines.append(line)
            if w := h.get("warn"):
                lines.append(
                    f"Achtung Wärmepumpe: Arbeitszahl {_cop(h['cop'])} – bei ähnlichem Wetter "
                    f"sonst {_cop(w['usual'])}. Heizkurve, Filter und Abtauung prüfen."
                )
        if line := WeeklyReport.curve_text((data.get("heating") or {}).get("curve")):
            lines.append(line)
        if w := data.get("water"):
            d = _delta(w["liters"], w.get("prev"), to)
            line = f"Wasser: {_liters(w['liters'])}" + (f" ({d})" if d else "")
            lines.append(line + yr(w["liters"], w.get("year")))
        if t := data.get("weather"):
            line = f"Wetter: Ø {_deg(t['mean'])}"
            if t.get("min") is not None and t.get("max") is not None:
                line += f", {_deg(t['min'])} bis {_deg(t['max'])}"
            if (t.get("prev") or {}).get("mean") is not None:
                diff = t["mean"] - t["prev"]["mean"]
                ref = "der Vormonat" if month else "die Vorwoche"
                if abs(diff) >= 0.5:
                    word = "wärmer" if diff > 0 else "kälter"
                    line += f" ({_deg(abs(diff)).rstrip('°')} Grad {word} als {ref})"
                else:
                    line += f" (wie {ref})"
            if month and (t.get("year") or {}).get("mean") is not None:
                line += f", Vorjahr Ø {_deg(t['year']['mean'])}"
            lines.append(line)
        ev = data.get("events") or {}
        happen = []
        if ev.get("alarm"):
            happen.append(f"Alarm ausgelöst ({ev['alarm']})")
        if ev.get("rings"):
            happen.append(f"{ev['rings']}× geklingelt")
        if ev.get("post"):
            happen.append(f"{ev['post']}× Post")
        if ev.get("done"):
            happen.append(f"{ev['done']} {'Aufgabe' if ev['done'] == 1 else 'Aufgaben'} erledigt")
        mowed, vacuumed = data.get("mowed"), data.get("vacuumed")
        if mowed:
            happen.append(f"{mowed} {'Fläche' if mowed == 1 else 'Flächen'} gemäht")
        if vacuumed:
            happen.append(f"{vacuumed} {'Raum' if vacuumed == 1 else 'Räume'} gesaugt")
        if happen:
            lines.append(" · ".join(happen))
        if data.get("due"):
            names = ", ".join(data.get("due_names") or [])
            more = data["due"] - len(data.get("due_names") or [])
            lines.append(
                f"Nächste Woche fällig: {names}" + (f" und {more} weitere" if more > 0 else "")
            )
        # two comparisons in a row in one bracket: "(−8 % zum Vormonat, +5 % zum Vorjahr)"
        return [line.replace(") (", ", ") for line in lines]

    async def async_run(self, push: bool = True, remember: bool = False) -> dict[str, Any]:
        """Collect, keep and send the report (remember: keep the heat pump's week)."""
        end = dt_util.now()
        data = await self.collect(end)
        if remember:
            self._remember(data, end - timedelta(days=7))
        lines = self.text(data)
        report = {"at": now_ms(), "title": "Wochenbericht", "lines": lines, "data": data}
        self.store.data["report"] = report
        self.store.save(SIGNAL_DATA)
        cfg = (self.store.data["notify"].get("sources") or {}).get(SRC, {"push": True})
        if push and lines and cfg.get("push", True):
            self.engine.send("Wochenbericht", "\n".join(lines), src=SRC, url="/", tag="report")
        return report

    def info(self) -> dict[str, Any] | None:
        """Return the last report for the dashboard."""
        return self.store.data.get("report")

    async def async_run_month(self, push: bool = True) -> dict[str, Any]:
        """Collect, keep and send the balance of the month before."""
        now = dt_util.as_local(dt_util.now())
        end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        start = (end - timedelta(days=1)).replace(day=1)
        p_start = (start - timedelta(days=1)).replace(day=1)
        y_start = start.replace(year=start.year - 1)
        y_end = end.replace(year=end.year - 1)
        data = await self.collect_period(start, end, p_start, start, y_start, y_end)
        data["month"] = f"{MONTHS[start.month - 1]} {start.year}"
        lines = self.text(data, month=True)
        title = f"Monatsbilanz {data['month']}"
        report = {"at": now_ms(), "title": title, "lines": lines, "data": data, "month": True}
        self.store.data["report_month"] = report
        self.store.save(SIGNAL_DATA)
        cfg = (self.store.data["notify"].get("sources") or {}).get(SRC, {"push": True})
        if push and lines and cfg.get("push", True):
            self.engine.send(title, "\n".join(lines), src=SRC, url="/", tag="report_month")
        return report

    def info_month(self) -> dict[str, Any] | None:
        """Return the last monthly balance for the dashboard."""
        return self.store.data.get("report_month")
