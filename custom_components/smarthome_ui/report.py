"""Weekly report: on Sunday evening a short summary of the last seven days.

Content (whatever is available): electricity used in the house, from the grid and fed in – with
costs from the prices of the energy dashboard –, solar yield and self-sufficiency, mowed areas,
vacuumed rooms and the tasks due next week. Sent as a push (source ``home.report``, "Wochenbericht"
in the dashboard's notifications, push on by default) every Sunday at 18:12 and kept as
``report`` for the dashboard (shown until Monday evening).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
import logging
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


def _eur(value: float) -> str:
    return f"{value:.2f} €".replace(".", ",")


def _kwh(value: float) -> str:
    return f"{value:.0f} kWh" if value >= 10 else f"{value:.1f} kWh".replace(".", ",")


class WeeklyReport:
    """Collect the week and send it."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        engine: NotifyEngine,
        energy: Callable[[], dict[str, Any]],
        tasks: Callable[[], list[dict[str, Any]]],
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self.engine = engine
        self.energy = energy
        self.tasks = tasks
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

    async def _change(self, statistic_id: str | None, start: datetime, end: datetime) -> float:
        """Return how much a meter counted between start and end (kWh, 0 if unknown)."""
        if not statistic_id:
            return 0.0
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
                {"change"},
                {"energy": "kWh"},
            )
        except Exception:  # noqa: BLE001 - recorder not available or no statistics
            return 0.0
        return float((res or {}).get("change") or 0)

    def _price(self, kind: str, default: float) -> float:
        prefs = self.energy()
        if (ent := prefs.get(f"price_{kind}_entity")) and (st := self.hass.states.get(ent)):
            try:
                return float(st.state)
            except ValueError:
                pass
        value = prefs.get(f"price_{kind}")
        return float(value) if value is not None else default

    async def collect(self, end: datetime | None = None) -> dict[str, Any]:
        """Return the numbers of the seven days before ``end``."""
        end = end or dt_util.now()
        start = end - timedelta(days=7)
        prefs = self.energy()

        async def total(kind: str) -> float:
            return sum([await self._change(s, start, end) for s in prefs.get(kind) or []])

        imp, exp, pv = await total("import"), await total("export"), await total("pv")
        charge, discharge = await total("battery_charge"), await total("battery_discharge")
        out: dict[str, Any] = {}
        if imp or exp or pv:
            used = max(0.0, imp - exp + pv + discharge - charge)
            p_imp, p_exp = self._price("import", 0.35), self._price("export", 0.08)
            out["energy"] = {
                "used": round(used, 1),
                "import": round(imp, 1),
                "export": round(exp, 1),
                "pv": round(pv, 1),
                "paid": round(imp * p_imp, 2),
                "earned": round(exp * p_exp, 2),
                "saved": round(max(0.0, pv - exp) * p_imp, 2),
                "autarky": round(100 * (1 - imp / used)) if used else None,
            }
        since = int(start.timestamp() * 1000)
        mowed = [
            a
            for a, r in (self.store.data["data"].get("mow_log") or {}).items()
            if (r or {}).get("last", 0) >= since
        ]
        cleaned = (self.store.data["data"].get("household") or {}).get("cleaned") or {}
        vacuumed = [
            a
            for a, r in cleaned.items()
            if max((r or {}).get("r", 0), (r or {}).get("w", 0)) >= since
        ]
        week_end = now_ms() + 7 * DAY_MS
        due = [
            t
            for t in task_states(self.tasks(), self.store.done)
            if t.due is not None and t.due <= week_end
        ]
        out["mowed"] = len(mowed)
        out["vacuumed"] = len(vacuumed)
        out["due"] = len(due)
        out["due_names"] = [t.name for t in sorted(due, key=lambda t: t.due or 0)[:3]]
        return out

    @staticmethod
    def text(data: dict[str, Any]) -> list[str]:
        """Return the report as short lines."""
        lines: list[str] = []
        if e := data.get("energy"):
            used, grid, paid = _kwh(e["used"]), _kwh(e["import"]), _eur(e["paid"])
            lines.append(f"Strom: {used} verbraucht, {grid} aus dem Netz ({paid})")
            if e["pv"]:
                fed, earned = _kwh(e["export"]), _eur(e["earned"])
                pv = f"Solar: {_kwh(e['pv'])}, {fed} eingespeist (+{earned})"
                if e.get("autarky") is not None:
                    pv += f", Autarkie {e['autarky']} %"
                lines.append(pv)
        robots = []
        if data.get("mowed"):
            robots.append(f"{data['mowed']} {'Fläche' if data['mowed'] == 1 else 'Flächen'} gemäht")
        if data.get("vacuumed"):
            robots.append(
                f"{data['vacuumed']} {'Raum' if data['vacuumed'] == 1 else 'Räume'} gesaugt"
            )
        if robots:
            lines.append(" · ".join(robots))
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
