"""Watcher (Wächter): keeps an eye on Home Assistant itself.

* **restarts** – every minute Core notes that Home Assistant is alive (``alive``, written every
  2 minutes). A start after a clean stop is a normal restart; a start without one means Home
  Assistant hung (the Supervisor's watchdog restarted it) or crashed. Then Core tells how long it
  was gone (source "Home Assistant" ``sys.health``, push on).
* **response time** – every 10 seconds Core measures how late a timer runs. A late timer means
  Home Assistant was busy (the event loop was blocked); from 30 seconds on Core notes a hang and
  tells (at most every 6 hours).
* **memory and load** – available memory and the load of the computer (``/proc``) every minute;
  per hour the worst value, kept two weeks; a push when less than 5 % memory is left.
* **Zigbee self-repair** – when Home Assistant (or the MQTT broker, which logs in through Home
  Assistant) is gone for a while, Zigbee2MQTT cannot publish that its devices are back, and they
  stay "unavailable" in Home Assistant although they work. 5 minutes after a start (and 3 minutes
  after the bridge reconnects) Core counts the Zigbee devices that are unavailable; if more are
  than before, it restarts Zigbee2MQTT (``<base>/bridge/request/restart``, at most every 6 hours)
  and says so.
* **database** – every night at 03:37 (and on request) Core counts how many states each entity
  wrote to the recorder in the last 24 hours, with the size of the database. The dashboard shows
  the biggest writers and can keep their history shorter or switch diagnostic entities off.

Stored as ``guard``: ``alive``, ``clean_stop``, ``events`` (60 days), ``hours`` (14 days), the last
database report ``db``, options (``heal``, ``z2m_base``).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
import logging
import os
import re
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_time_change,
    async_track_time_interval,
)

from .const import SIGNAL_DATA
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

SRC = "sys.health"
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS
SAVE_EVERY_MIN = 2
LAG_EVERY_S = 10
LAG_HANG_S = 30
MEM_LOW_PCT = 5
HEAL_AFTER_START_S = 300
HEAL_AFTER_BRIDGE_S = 180
HEAL_RECHECK_S = 240
HEAL_GAP_MS = 6 * HOUR_MS
WARN_GAP_MS = 6 * HOUR_MS
KEEP_EVENTS_MS = 60 * DAY_MS
KEEP_HOURS_MS = 14 * DAY_MS
DB_RUN_AT = (3, 37)
DB_WINDOW_H = 24
DB_TOP = 40
OPTIONS = {"heal": True, "z2m_base": "zigbee2mqtt"}
# diagnostic values that change often and are rarely looked at again
_CHATTY = re.compile(
    r"(linkquality|link_quality|_rssi|_lqi|signal|_uptime|betriebszeit|_rx$|_tx$|last_seen|"
    r"verbindungsgeschwindigkeit|link_speed|_cpu|_memory|speicherauslastung|auslastung)",
    re.IGNORECASE,
)


def _read_proc() -> dict[str, float] | None:
    """Return available memory (MB, %) and the load per CPU (%) of the computer."""
    try:
        mem: dict[str, int] = {}
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, rest = line.partition(":")
                if key in ("MemTotal", "MemAvailable"):
                    mem[key] = int(rest.split()[0])
        with open("/proc/loadavg", encoding="utf-8") as f:
            load = float(f.read().split()[0])
        cpus = os.cpu_count() or 1
    except (OSError, ValueError, IndexError):
        return None
    total, avail = mem.get("MemTotal"), mem.get("MemAvailable")
    if not total or avail is None:
        return None
    return {
        "total_mb": round(total / 1024),
        "avail_mb": round(avail / 1024),
        "avail_pct": round(avail / total * 100, 1),
        "load_pct": round(load / cpus * 100),
    }


class Guard:
    """Watch Home Assistant itself: restarts, hangs, memory, Zigbee2MQTT, the database."""

    def __init__(
        self,
        hass: HomeAssistant,
        store: CoreStore,
        notify: Callable[[str, str, str], None],
        history: Any = None,
    ) -> None:
        """Initialize (notify(title, message, tag))."""
        self.hass = hass
        self.store = store
        self.notify = notify
        self.history = history
        self._unsub: list[CALLBACK_TYPE] = []
        self._bridge_unsub: CALLBACK_TYPE | None = None
        self._ticks = 0
        self._lag_due: float | None = None
        self._lag_handle: CALLBACK_TYPE | None = None
        self.proc: dict[str, float] | None = None
        self.started = now_ms()
        self.read_proc: Callable[[], dict[str, float] | None] = _read_proc

    # ---------------------------------------------------------------- data

    @property
    def data(self) -> dict[str, Any]:
        """Return the stored data."""
        return self.store.data.setdefault("guard", {})

    def options(self) -> dict[str, Any]:
        """Return the options (self-repair on, MQTT base topic of Zigbee2MQTT)."""
        return {**OPTIONS, **(self.data.get("options") or {})}

    @callback
    def set_options(self, options: dict[str, Any]) -> None:
        """Change options."""
        self.data["options"] = {**self.options(), **options}
        self.store.save(SIGNAL_DATA)

    @property
    def events(self) -> list[dict[str, Any]]:
        """Return the kept events (oldest first)."""
        return self.data.setdefault("events", [])

    @callback
    def _event(self, kind: str, **extra: Any) -> dict[str, Any]:
        ev = {"t": now_ms(), "kind": kind, **extra}
        keep = [e for e in self.events if e.get("t", 0) > now_ms() - KEEP_EVENTS_MS]
        self.data["events"] = [*keep, ev][-200:]
        self.store.save(SIGNAL_DATA)
        return ev

    def _recent(self, kind: str, gap_ms: int) -> bool:
        return any(e["kind"] == kind and e["t"] > now_ms() - gap_ms for e in self.events)

    # ---------------------------------------------------------------- lifecycle

    @callback
    def async_setup(self) -> None:
        """Note the start, start the checks."""
        self.started = now_ms()
        alive, clean = self.data.get("alive"), self.data.get("clean_stop")
        if alive:
            down = max(0, round((self.started - int(alive)) / 1000))
            self._event("start", clean=bool(clean), down=down, last=alive)
            if not clean:
                mins = max(1, round(down / 60))
                self.notify(
                    "Home Assistant",
                    f"Home Assistant ist neu gestartet, ohne vorher sauber herunterzufahren – "
                    f"etwa {mins} Min. weg. Es hing oder ist abgestürzt.",
                    "guard|start",
                )
        # baseline of unavailable Zigbee devices from before (the last snapshot of the last run)
        self._z2m_before = int(self.data.get("z2m_off") or 0)
        self.data.update({"alive": self.started, "clean_stop": False, "started": self.started})
        self.store.save(SIGNAL_DATA)
        self._unsub.append(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._stopping)
        )
        self._unsub.append(async_track_time_interval(self.hass, self._tick, timedelta(minutes=1)))
        self._unsub.append(
            async_track_time_change(
                self.hass, self._nightly, hour=DB_RUN_AT[0], minute=DB_RUN_AT[1], second=0
            )
        )
        self._unsub.append(
            async_call_later(
                self.hass, HEAL_AFTER_START_S, lambda _now: self._heal_check("start", 0)
            )
        )
        self._schedule_lag()
        self._watch_bridge()

    @callback
    def async_stop(self) -> None:
        """Stop; a stop of the integration counts as a clean stop."""
        self._mark_clean()
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._bridge_unsub:
            self._bridge_unsub()
            self._bridge_unsub = None
        if self._lag_handle:
            self._lag_handle()
            self._lag_handle = None

    @callback
    def _stopping(self, _event: Event) -> None:
        self._mark_clean()

    @callback
    def _mark_clean(self) -> None:
        self.data.update({"alive": now_ms(), "clean_stop": True})
        self.store.save()

    # ---------------------------------------------------------------- every minute

    @callback
    def _tick(self, _now: Any = None) -> None:
        self._ticks += 1
        self.data["alive"] = now_ms()
        self.hass.async_create_task(self._async_sample())
        if self._ticks % 5 == 0:
            self._snapshot_z2m()
        if self._ticks % SAVE_EVERY_MIN == 0:
            self.store.save()

    async def _async_sample(self) -> None:
        proc = await self.hass.async_add_executor_job(self.read_proc)
        if not proc:
            return
        self.proc = proc
        self._hour(mem=proc["avail_pct"], load=proc["load_pct"])
        if proc["avail_pct"] < MEM_LOW_PCT and not self._recent("mem_low", WARN_GAP_MS):
            self._event("mem_low", pct=proc["avail_pct"], mb=proc["avail_mb"])
            self.notify(
                "Home Assistant",
                f"Nur noch {proc['avail_mb']} MB Arbeitsspeicher frei "
                f"({proc['avail_pct']:.0f} %) – Home Assistant kann gleich hängen.".replace(
                    ".0 %", " %"
                ),
                "guard|mem",
            )

    @callback
    def _hour(
        self, *, lag: float | None = None, mem: float | None = None, load: float | None = None
    ) -> None:
        """Keep the worst value of the hour (lag: highest, memory: lowest, load: highest)."""
        hour = now_ms() // HOUR_MS * HOUR_MS
        hours = [h for h in self.data.setdefault("hours", []) if h["t"] > now_ms() - KEEP_HOURS_MS]
        cur = hours[-1] if hours and hours[-1]["t"] == hour else None
        if cur is None:
            cur = {"t": hour}
            hours.append(cur)
        if lag is not None:
            cur["lag"] = round(max(cur.get("lag") or 0, lag), 1)
        if mem is not None:
            cur["mem"] = mem if cur.get("mem") is None else min(cur["mem"], mem)
        if load is not None:
            cur["load"] = max(cur.get("load") or 0, load)
        self.data["hours"] = hours

    # ---------------------------------------------------------------- response time

    @callback
    def _schedule_lag(self) -> None:
        self._lag_due = self.hass.loop.time() + LAG_EVERY_S
        self._lag_handle = async_call_later(self.hass, LAG_EVERY_S, self._lag_tick)

    @callback
    def _lag_tick(self, _now: Any) -> None:
        if self._lag_due is not None:
            self.lag(max(0.0, self.hass.loop.time() - self._lag_due))
        self._schedule_lag()

    @callback
    def lag(self, seconds: float) -> None:
        """Take one measurement: how many seconds a timer ran late."""
        self._hour(lag=seconds)
        if seconds >= LAG_HANG_S:
            self._event("hang", s=round(seconds))
            if not self._recent("hang_told", WARN_GAP_MS):
                self._event("hang_told")
                self.notify(
                    "Home Assistant",
                    f"Home Assistant hat {round(seconds)} Sekunden lang nicht reagiert.",
                    "guard|hang",
                )

    # ---------------------------------------------------------------- Zigbee2MQTT

    def z2m(self) -> dict[str, Any]:
        """Return the bridge entity and the Zigbee devices that are unavailable."""
        ents, devs = er.async_get(self.hass), dr.async_get(self.hass)
        bridge: str | None = None
        states: dict[str, list[str | None]] = {}
        names: dict[str, str] = {}
        for entry in ents.entities.values():
            if entry.platform != "mqtt" or entry.disabled_by or not entry.device_id:
                continue
            dev = devs.async_get(entry.device_id)
            ids = [str(i[1]) for i in (dev.identifiers if dev else ()) if i[0] == "mqtt"]
            if not any(i.startswith("zigbee2mqtt_") for i in ids):
                continue
            if any(i.startswith("zigbee2mqtt_bridge") for i in ids):
                if entry.domain == "binary_sensor" and "connection_state" in entry.entity_id:
                    bridge = entry.entity_id
                continue
            if entry.entity_category:
                continue
            st = self.hass.states.get(entry.entity_id)
            states.setdefault(entry.device_id, []).append(st.state if st else None)
            if dev:
                names[entry.device_id] = dev.name_by_user or dev.name or entry.device_id
        offline = [d for d, ss in states.items() if ss and all(s == "unavailable" for s in ss)]
        bstate = self.hass.states.get(bridge) if bridge else None
        return {
            "bridge": bridge,
            "bridge_on": bstate is not None and bstate.state == "on",
            "devices": len(states),
            "offline": len(offline),
            "names": sorted(names.get(d, d) for d in offline)[:8],
        }

    @callback
    def _snapshot_z2m(self) -> None:
        """Remember how many Zigbee devices are unavailable while everything runs normally."""
        info = self.z2m()
        if info["bridge"] and info["bridge_on"] and now_ms() - self.started > 10 * 60_000:
            self.data["z2m_off"] = info["offline"]

    @callback
    def _watch_bridge(self) -> None:
        bridge = self.z2m()["bridge"]
        if not bridge:
            return

        @callback
        def changed(event: Event) -> None:
            old, new = event.data.get("old_state"), event.data.get("new_state")
            if new is None or new.state != "on" or (old is not None and old.state == "on"):
                return
            down_s = (new.last_changed - old.last_changed).total_seconds() if old else 0
            if down_s < 30:
                return
            before = int(self.data.get("z2m_off") or 0)
            self._unsub.append(
                async_call_later(
                    self.hass,
                    HEAL_AFTER_BRIDGE_S,
                    lambda _now: self._heal_check("bridge", before),
                )
            )

        self._bridge_unsub = async_track_state_change_event(self.hass, [bridge], changed)

    @callback
    def _heal_check(self, reason: str, before: int, tries: int = 0) -> None:
        """Restart Zigbee2MQTT when more Zigbee devices are unavailable than before."""
        if reason == "start":
            before = self._z2m_before
        info = self.z2m()
        if not info["bridge"]:
            return
        if not info["bridge_on"]:
            if tries < 3:
                self._unsub.append(
                    async_call_later(
                        self.hass, 120, lambda _now: self._heal_check(reason, before, tries + 1)
                    )
                )
            return
        if info["offline"] <= before or info["offline"] == 0:
            return
        if not self.options()["heal"]:
            self._event("heal_off", reason=reason, offline=info["offline"], names=info["names"])
            return
        if self._recent("heal", HEAL_GAP_MS):
            return
        self.hass.async_create_task(self.async_heal(reason, info))

    async def async_heal(self, reason: str = "manual", info: dict[str, Any] | None = None) -> bool:
        """Restart Zigbee2MQTT (MQTT request), note and tell it."""
        info = info or self.z2m()
        base = str(self.options()["z2m_base"] or "zigbee2mqtt").strip("/")
        if not self.hass.services.has_service("mqtt", "publish"):
            return False
        try:
            await self.hass.services.async_call(
                "mqtt",
                "publish",
                {"topic": f"{base}/bridge/request/restart", "payload": ""},
                blocking=True,
            )
        except Exception:  # noqa: BLE001 - broker not reachable
            _LOGGER.warning("Could not ask Zigbee2MQTT to restart", exc_info=True)
            return False
        ev = self._event("heal", reason=reason, offline=info["offline"], names=info["names"])
        if reason != "manual":
            why = {
                "start": "nach dem Neustart von Home Assistant",
                "bridge": "nachdem die Verbindung zum MQTT-Broker weg war",
            }.get(reason, "")
            n = info["offline"]
            self.notify(
                "Zigbee",
                f"Zigbee2MQTT neu gestartet: {n} {'Gerät war' if n == 1 else 'Geräte waren'} "
                f"{why} nicht erreichbar.".replace("  ", " "),
                "guard|heal",
            )

        @callback
        def result(_now: Any) -> None:
            ev["after"] = self.z2m()["offline"]
            self.store.save(SIGNAL_DATA)

        self._unsub.append(async_call_later(self.hass, HEAL_RECHECK_S, result))
        return True

    # ---------------------------------------------------------------- database

    @callback
    def _nightly(self, _now: Any) -> None:
        self.hass.async_create_task(self.async_db_scan())

    async def async_db_scan(self) -> dict[str, Any] | None:
        """Count the states each entity wrote in the last 24 hours; keep and return the report."""
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415

            rec = get_instance(self.hass)
        except Exception:  # noqa: BLE001 - recorder not loaded
            return None
        since = now_ms() / 1000 - DB_WINDOW_H * 3600
        try:
            raw = await rec.async_add_executor_job(_db_query, self.hass, since)
        except Exception:  # noqa: BLE001 - other database, query failed
            _LOGGER.warning("Database report failed", exc_info=True)
            return None
        report = self._db_report(raw)
        self.data["db"] = report
        self.store.save(SIGNAL_DATA)
        return report

    def _db_report(self, raw: dict[str, Any]) -> dict[str, Any]:
        ents, devs = er.async_get(self.hass), dr.async_get(self.hass)
        total = int(raw.get("total") or 0)
        states_size = raw.get("states_size")
        own = (self.history.entities if self.history is not None else {}) or {}
        top = []
        for entity_id, count in raw.get("top") or []:
            entry = ents.async_get(entity_id)
            st = self.hass.states.get(entity_id)
            dev = devs.async_get(entry.device_id) if entry and entry.device_id else None
            diag = bool(entry and entry.entity_category)
            chatty = diag or bool(_CHATTY.search(entity_id))
            top.append(
                {
                    "entity_id": entity_id,
                    "name": (st.attributes.get("friendly_name") if st else None) or entity_id,
                    "device": (dev.name_by_user or dev.name) if dev else None,
                    "count": int(count),
                    "pct": round(count / total * 100, 1) if total else 0,
                    "bytes": round(states_size * count / total) if states_size and total else None,
                    "diagnostic": diag,
                    "suggest": "disable" if chatty and entry is not None else "short",
                    "keep": own.get(entity_id),
                    "disabled": bool(entry and entry.disabled_by),
                    "in_registry": entry is not None,
                }
            )
        return {
            "at": now_ms(),
            "window_h": DB_WINDOW_H,
            "total": total,
            "size": raw.get("size"),
            "states_size": states_size,
            "dialect": raw.get("dialect"),
            "entities": raw.get("entities"),
            "top": top,
        }

    # ---------------------------------------------------------------- dashboard

    def summary(self, since: int, until: int) -> dict[str, Any]:
        """Return what happened between since and until (weekly report)."""
        evs = [e for e in self.events if since <= e.get("t", 0) < until]
        starts = [e for e in evs if e["kind"] == "start"]
        unclean = [e for e in starts if not e.get("clean")]
        return {
            "restarts": len(starts),
            "unclean": len(unclean),
            "down_s": sum(int(e.get("down") or 0) for e in unclean),
            "hangs": len([e for e in evs if e["kind"] == "hang"]),
            "heals": len([e for e in evs if e["kind"] == "heal"]),
        }

    def info(self) -> dict[str, Any]:
        """Return everything for the dashboard."""
        week = self.summary(now_ms() - 7 * DAY_MS, now_ms() + 1)
        return {
            "started": self.started,
            "events": [e for e in self.events if e["kind"] != "hang_told"][-40:][::-1],
            "hours": self.data.get("hours") or [],
            "proc": self.proc,
            "week": week,
            "z2m": self.z2m(),
            "options": self.options(),
            "db": self.data.get("db"),
        }


def _db_query(hass: HomeAssistant, since: float) -> dict[str, Any]:
    """Count states per entity since ``since`` and read the size of the database (executor)."""
    from sqlalchemy import text  # noqa: PLC0415

    from homeassistant.components.recorder import get_instance  # noqa: PLC0415
    from homeassistant.components.recorder.util import session_scope  # noqa: PLC0415

    rec = get_instance(hass)
    dialect = rec.engine.dialect.name if rec.engine else None
    out: dict[str, Any] = {"dialect": dialect}
    with session_scope(hass=hass, read_only=True) as session:
        out["total"] = session.execute(
            text("SELECT COUNT(*) FROM states WHERE last_updated_ts > :since"), {"since": since}
        ).scalar()
        out["top"] = [
            (row[0], row[1])
            for row in session.execute(
                text(
                    "SELECT m.entity_id, COUNT(*) AS n FROM states s "
                    "JOIN states_meta m ON s.metadata_id = m.metadata_id "
                    "WHERE s.last_updated_ts > :since "
                    "GROUP BY m.entity_id ORDER BY n DESC LIMIT :top"
                ),
                {"since": since, "top": DB_TOP},
            ).all()
        ]
        out["entities"] = session.execute(text("SELECT COUNT(*) FROM states_meta")).scalar()
        if dialect in ("mysql", "mariadb"):
            rows = session.execute(
                text(
                    "SELECT table_name, data_length + index_length FROM information_schema.tables "
                    "WHERE table_schema = DATABASE()"
                )
            ).all()
            sizes = {str(r[0]): int(r[1] or 0) for r in rows}
            out["size"] = sum(sizes.values())
            out["states_size"] = sizes.get("states")
        elif dialect == "sqlite":
            pages = session.execute(text("PRAGMA page_count")).scalar() or 0
            page = session.execute(text("PRAGMA page_size")).scalar() or 0
            out["size"] = int(pages) * int(page)
            try:
                out["states_size"] = session.execute(
                    text("SELECT SUM(pgsize) FROM dbstat WHERE name IN ('states')")
                ).scalar()
            except Exception:  # noqa: BLE001 - dbstat not compiled in
                out["states_size"] = None
    return out
