"""Tasks (cleaning, maintenance, garden) and when they are due.

The dashboard decides which tasks exist and when they are due (it knows robot runs, seasons
and intervals) and sends them with ``tasks/sync``. If a task is done here – via the to-do
list or an action – before the dashboard syncs again, the next due date is the completion
time plus the interval.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from homeassistant.util import dt as dt_util

DAY_MS = 86_400_000


@dataclass(frozen=True, slots=True)
class TaskState:
    """A task with its current due date."""

    key: str
    name: str
    src: str
    area: str | None
    icon: str | None
    interval: float | None
    last: int | None
    due: int | None
    by: str | None
    who: str | None

    @property
    def due_datetime(self) -> datetime | None:
        """Return the due date as an aware datetime."""
        return None if self.due is None else dt_util.utc_from_timestamp(self.due / 1000)

    @property
    def due_date(self) -> date | None:
        """Return the local day the task is due."""
        due = self.due_datetime
        return None if due is None else dt_util.as_local(due).date()

    @property
    def last_datetime(self) -> datetime | None:
        """Return when the task was last done."""
        return None if not self.last else dt_util.utc_from_timestamp(self.last / 1000)


def _num(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def task_states(tasks: list[dict[str, Any]], done: dict[str, dict[str, Any]]) -> list[TaskState]:
    """Combine the synced task definitions with the done records."""
    states: list[TaskState] = []
    for task in tasks:
        key = task["key"]
        interval = _num(task.get("interval"))
        last = int(_num(task.get("last")) or 0) or None
        due = int(_num(task.get("due")) or 0) or None
        record = done.get(key) or {}
        by, who = record.get("by"), record.get("who")
        stamp = int(_num(record.get("t")) or 0)
        if stamp and stamp > (last or 0) + 1000:
            # done here after the dashboard last synced: next date from the interval
            last = stamp
            due = stamp + int(interval * DAY_MS) if interval else None
        states.append(
            TaskState(
                key=key,
                name=str(task["name"]),
                src=str(task.get("src") or "hh.tasks"),
                area=task.get("area"),
                icon=task.get("icon"),
                interval=interval,
                last=last,
                due=due,
                by=by,
                who=who,
            )
        )
    return states


def due_today(state: TaskState, today: date) -> bool:
    """Return True if the task is due today or overdue."""
    return state.due_date is not None and state.due_date <= today


def find_task(states: list[TaskState], value: str) -> TaskState | None:
    """Find a task by key or (case-insensitive) name."""
    folded = value.strip().casefold()
    for state in states:
        if state.key == value or state.name.casefold() == folded:
            return state
    return None
