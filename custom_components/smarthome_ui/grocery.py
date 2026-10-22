"""Grocery spending per month: the "last month" sensor's value is kept per month.

Shopping integrations (e.g. receipts from the supermarket app) give "this month" and "last month"
but no history further back. The dashboard tells which sensor is "last month"
(``grocery/settings``); whenever it changes (and at start) Core keeps its value for the previous
month in ``grocery``: ``{"entity": str, "months": {"YYYY-MM": value}}``.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import CALLBACK_TYPE, Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import SIGNAL_DATA
from .store import CoreStore

MONTHS_MAX = 36


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def prev_month_key() -> str:
    """Return the key of last month (YYYY-MM)."""
    now = dt_util.now()
    year, month = (now.year, now.month - 1) if now.month > 1 else (now.year - 1, 12)
    return f"{year}-{month:02d}"


class Grocery:
    """Keep the monthly grocery spending."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self._unsub: CALLBACK_TYPE | None = None

    @property
    def data(self) -> dict[str, Any]:
        """Return entity and months."""
        return self.store.data["grocery"]

    @callback
    def set_settings(self, entity: str | None, months: dict[str, float] | None = None) -> None:
        """Take the "last month" sensor (and months the dashboard kept so far, once)."""
        self.data["entity"] = entity
        if months:
            have = self.data.setdefault("months", {})
            for key, value in months.items():
                if key not in have and _num(value) is not None:
                    have[key] = float(value)
        self._listen()
        self.record()
        self.store.save(SIGNAL_DATA)

    @callback
    def async_setup(self) -> None:
        """Start listening."""
        self._listen()
        self.record()

    @callback
    def async_stop(self) -> None:
        """Stop."""
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _listen(self) -> None:
        self.async_stop()
        if not (entity := self.data.get("entity")):
            return

        @callback
        def relevant(data: EventStateChangedData) -> bool:
            return data["entity_id"] == entity

        @callback
        def changed(_event: Event[EventStateChangedData]) -> None:
            self.record()

        self._unsub = self.hass.bus.async_listen(
            EVENT_STATE_CHANGED, changed, event_filter=relevant
        )

    @callback
    def record(self) -> None:
        """Keep the current value of "last month"."""
        state = self.hass.states.get(self.data.get("entity") or "")
        value = _num(state.state) if state else None
        if value is None:
            return
        months = self.data.setdefault("months", {})
        key = prev_month_key()
        if months.get(key) is not None and abs(months[key] - value) <= 0.005:
            return
        months[key] = value
        for old in sorted(months)[:-MONTHS_MAX]:
            months.pop(old)
        self.store.save(SIGNAL_DATA)

    def info(self) -> dict[str, Any]:
        """Return what the dashboard needs."""
        return {"entity": self.data.get("entity"), "months": dict(self.data.get("months") or {})}
