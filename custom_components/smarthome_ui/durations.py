"""Usual duration per area / room and a warning when a run is clearly shorter or longer.

The last ``KEEP`` durations (minutes) per robot and area are kept; "usual" is their median once
there are at least ``MIN_RUNS``. A run counts as unusual if it takes less than half or more than
1.8 times as long as usual – and differs by at least a few minutes (short rooms vary a lot).
"""

from __future__ import annotations

from statistics import median
from typing import Any

from .store import CoreStore

KEEP = 10
MIN_RUNS = 3
SHORT, LONG = 0.5, 1.8


def history(store: CoreStore, robot_id: str, area: str) -> list[int]:
    """Return the last durations (minutes) of an area."""
    return list(((store.data["durations"].get(robot_id) or {}).get(area)) or [])


def usual(store: CoreStore, robot_id: str, area: str) -> int | None:
    """Return the usual duration (median) of an area, None with too few runs."""
    runs = history(store, robot_id, area)
    return round(median(runs)) if len(runs) >= MIN_RUNS else None


def record(
    store: CoreStore, robot_id: str, area: str, minutes: int, min_diff: int
) -> dict[str, Any] | None:
    """Add a run; return {"usual", "minutes", "kind": "short"|"long"} if it was unusual."""
    before = usual(store, robot_id, area)
    runs = [*history(store, robot_id, area), int(minutes)][-KEEP:]
    store.data["durations"].setdefault(robot_id, {})[area] = runs
    store.save()
    if before is None or abs(minutes - before) < min_diff:
        return None
    if minutes < before * SHORT:
        return {"usual": before, "minutes": minutes, "kind": "short"}
    if minutes > before * LONG:
        return {"usual": before, "minutes": minutes, "kind": "long"}
    return None


def message(robot: str, area: str, result: dict[str, Any], mower: bool) -> str:
    """Return the warning text."""
    mins, usual_mins = result["minutes"], result["usual"]
    if result["kind"] == "short":
        hint = "nicht fertig geworden?"
        return f"{robot}: {area} nur {mins} min statt üblich {usual_mins} min – {hint}"
    hint = "festgefahren oder hohes Gras?" if mower else "viel Schmutz oder Hindernisse?"
    return f"{robot}: {area} {mins} min statt üblich {usual_mins} min – {hint}"
