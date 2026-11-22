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
DAY_MS = 86_400_000
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


def _delta(cur: float | None, prev: float | None) -> str:
    """Return the change against the week before ("+12 %", "−8 %", "gleich"); empty if unknown."""
    if cur is None or not prev or prev < 0.5:
        return ""
    pct = round((cur - prev) / prev * 100)
    if pct == 0:
        return "gleich wie Vorwoche"
    return f"{'+' if pct > 0 else '−'}{abs(pct)} % zur Vorwoche"


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

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _tick(self, now: datetime) -> None:
        if dt_util.as_local(now).weekday() == WEEKDAY:
            self.hass.async_create_task(self.async_run())

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
        """Return the daily sums of meters (kWh) for the seven days from start."""
        out = [0.0] * 7
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
                if 0 <= i < 7:
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
        p_start = start - timedelta(days=7)
        out: dict[str, Any] = {}
        if energy := await self._energy(start, end):
            energy["prev"] = await self._energy(p_start, start)
            # best solar day of the week (from the daily sums of the solar meters)
            days = await self._daily(self.energy().get("pv") or [], start, end)
            if max(days) > 0:
                i = days.index(max(days))
                energy["best_day"] = {
                    "day": WEEKDAYS[(start + timedelta(days=i)).weekday()],
                    "kwh": round(days[i], 1),
                }
            out["energy"] = energy
        heat = await self._heat(start, end)
        if heat is not None:
            out["heating"] = {"heatpump": heat, "prev": await self._heat(p_start, start)}
        water = await self._water(start, end)
        if water:
            out["water"] = {"liters": water, "prev": await self._water(p_start, start)}
        if weather := await self._weather(start, end):
            weather["prev"] = await self._weather(p_start, start)
            out["weather"] = weather
        since, until = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
        ev = self._events(since, until)
        ev["prev"] = self._events(int(p_start.timestamp() * 1000), since)
        out["events"] = ev
        week_end = now_ms() + 7 * DAY_MS
        due = [
            t
            for t in task_states(self.tasks(), self.store.done)
            if t.due is not None and t.due <= week_end
        ]
        # older fields, kept for the dashboard
        out["mowed"] = ev["mowed"]
        out["vacuumed"] = ev["vacuumed"]
        out["due"] = len(due)
        out["due_names"] = [t.name for t in sorted(due, key=lambda t: t.due or 0)[:3]]
        return out

    @staticmethod
    def text(data: dict[str, Any]) -> list[str]:
        """Return the report as short lines."""
        lines: list[str] = []

        def vs(cur: float | None, prev: dict[str, Any] | None, key: str) -> str:
            d = _delta(cur, (prev or {}).get(key))
            return f" ({d})" if d else ""

        if e := data.get("energy"):
            prev = e.get("prev")
            used, grid, paid = _kwh(e["used"]), _kwh(e["import"]), _eur(e["paid"])
            change = vs(e["used"], prev, "used")
            lines.append(f"Strom: {used} verbraucht{change}, {grid} aus dem Netz ({paid})")
            if e["pv"]:
                fed, earned = _kwh(e["export"]), _eur(e["earned"])
                change = vs(e["pv"], prev, "pv")
                pv = f"Solar: {_kwh(e['pv'])}{change}, {fed} eingespeist (+{earned})"
                if e.get("autarky") is not None:
                    pv += f", Autarkie {e['autarky']} %"
                lines.append(pv)
                if best := e.get("best_day"):
                    lines.append(f"Bester Sonnentag: {best['day']} ({_kwh(best['kwh'])})")
        if h := data.get("heating"):
            d = _delta(h["heatpump"], h.get("prev"))
            lines.append(
                f"Heizung: Wärmepumpe {_kwh(h['heatpump'])} Strom" + (f" ({d})" if d else "")
            )
        if w := data.get("water"):
            d = _delta(w["liters"], w.get("prev"))
            lines.append(f"Wasser: {_liters(w['liters'])}" + (f" ({d})" if d else ""))
        if t := data.get("weather"):
            line = f"Wetter: Ø {_deg(t['mean'])}"
            if t.get("min") is not None and t.get("max") is not None:
                line += f", {_deg(t['min'])} bis {_deg(t['max'])}"
            if (t.get("prev") or {}).get("mean") is not None:
                diff = t["mean"] - t["prev"]["mean"]
                if abs(diff) >= 0.5:
                    word = "wärmer" if diff > 0 else "kälter"
                    line += f" ({_deg(abs(diff)).rstrip('°')} Grad {word} als die Vorwoche)"
                else:
                    line += " (wie die Vorwoche)"
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
        return lines

    async def async_run(self, push: bool = True) -> dict[str, Any]:
        """Collect, keep and send the report."""
        data = await self.collect()
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
