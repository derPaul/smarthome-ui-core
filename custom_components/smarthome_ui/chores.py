"""Task definitions (maintenance, garden calendar, cleaning rooms) and their due dates.

The dashboard edits the definitions and sends them once after each change (``tasks/defs``). From
then on Core computes the due dates itself – from the definitions, the done records and the times
the robot vacuum cleaned – so sensors, to-do lists and notifications work without a dashboard.

A task: ``{"scope": "hh"|"gd", "id", "name", "icon", "src", "every": {"n", "unit": d|w|m}`` or
``"windows": [[from_month, to_month], ...]`` (a task in fixed months), ``"areas": [area | None]``,
``"names": {area: name}}``. A room: ``{"area", "name", "interval"}`` (days; the own interval from
``cleaning.intervals`` wins, 0 = not tracked).
"""

from __future__ import annotations

from calendar import monthrange
from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

DAY_MS = 86_400_000


def _local(ms: int) -> datetime:
    return dt_util.as_local(dt_util.utc_from_timestamp(ms / 1000))


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def add_every(ms: int, every: dict[str, Any], sign: int = 1) -> int:
    """Add an interval ({"n": 3, "unit": "m"}) to a time (months as calendar months)."""
    d = _local(ms)
    n = float(every.get("n") or 1)
    unit = every.get("unit") or "d"
    if unit == "m":
        months = d.month - 1 + sign * round(n)
        year, month = d.year + months // 12, months % 12 + 1
        d = d.replace(year=year, month=month, day=min(d.day, monthrange(year, month)[1]))
    else:
        d = d + timedelta(days=sign * n * (7 if unit == "w" else 1))
    return _ms(d)


def start_of_day(ms: int) -> int:
    """Return local midnight of the day of a time."""
    return _ms(_local(ms).replace(hour=0, minute=0, second=0, microsecond=0))


def interval_days(every: dict[str, Any] | None) -> float | None:
    """Return an interval in days (months ≈ 30.44 days)."""
    if not every:
        return None
    n = float(every.get("n") or 1)
    unit = every.get("unit") or "d"
    return round(n * 30.44, 1) if unit == "m" else n * 7 if unit == "w" else n


def windows_around(windows: list[list[int]], now_ms: int) -> list[tuple[int, int]]:
    """Return the month windows of last, this and next year as (start, end) ms."""
    tz = dt_util.get_default_time_zone()
    year = _local(now_ms).year
    out = []
    for y in (year - 1, year, year + 1):
        for a, b in windows:
            start = datetime(y, a, 1, tzinfo=tz)
            ey = y if b >= a else y + 1
            end = datetime(ey, b, monthrange(ey, b)[1], 23, 59, 59, tzinfo=tz)
            out.append((_ms(start), _ms(end)))
    return sorted(out)


def _done_at(done: dict[str, Any], key: str, legacy: int | None) -> int | None:
    """Done time of a key; the old storage counts while there is no record (like the dashboard)."""
    rec = done.get(key)
    if rec:
        return rec.get("t") or None
    return legacy or None


def _task_state(
    task: dict[str, Any], done_at: int | None, now: int
) -> tuple[int | None, int | None]:
    """Return (last, due) of one task in one area."""
    windows = task.get("windows")
    if windows:
        wins = windows_around(windows, now)
        cur = next((w for w in wins if w[0] <= now <= w[1]), None)
        if cur and not (done_at and done_at >= cur[0]):
            return done_at, cur[0]
        after = cur[1] if cur else now
        nxt = next((w for w in wins if w[0] > after), wins[-1])
        return done_at, nxt[0]
    every = task.get("every")
    if not done_at or not every:
        return done_at, None
    return done_at, start_of_day(add_every(done_at, every))


def compute(store_data: dict[str, Any], now: int) -> list[dict[str, Any]]:
    """Return all tasks with due dates in the shape of ``tasks/sync``."""
    defs = store_data.get("task_defs") or {}
    data = store_data["data"]
    done = data.get("done") or {}
    hh = data.get("household") or {}
    legacy_maint = hh.get("maint") or {}
    out: list[dict[str, Any]] = []
    for task in defs.get("tasks") or []:
        scope = task.get("scope") or "hh"
        src = task.get("src") or ("gd.tasks" if scope == "gd" else "hh.tasks")
        names = task.get("names") or {}
        for area in task.get("areas") or [None]:
            key = f"{scope}:task:{task['id']}|{area or ''}"
            legacy = legacy_maint.get(f"{task['id']}|{area or ''}") if scope == "hh" else None
            last, due = _task_state(task, _done_at(done, key, legacy), now)
            area_name = names.get(area) if area else None
            out.append(
                {
                    "key": key,
                    "name": f"{task['name']} · {area_name}" if area_name else task["name"],
                    "src": src,
                    "area": area_name,
                    "icon": task.get("icon"),
                    "interval": None if task.get("windows") else interval_days(task.get("every")),
                    "last": last,
                    "due": due,
                }
            )
    own = (store_data.get("cleaning") or {}).get("intervals") or {}
    cleaned = hh.get("cleaned") or {}
    for room in defs.get("rooms") or []:
        area = room["area"]
        interval = own.get(area, room.get("interval"))
        if not interval:
            continue
        times = cleaned.get(area) or {}
        manual = _done_at(done, f"clean:{area}", times.get("m"))
        last = max(times.get("r") or 0, manual or 0) or None
        out.append(
            {
                "key": f"clean:{area}",
                "name": f"{room.get('name') or area} putzen",
                "src": "hh.tasks",
                "area": room.get("name") or area,
                "icon": "mdi:broom",
                "interval": interval,
                "last": last,
                "due": int(last + float(interval) * DAY_MS) if last else None,
            }
        )
    return out
