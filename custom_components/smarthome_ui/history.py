"""History per device: keep the recorder history of single devices shorter or longer.

The recorder keeps every state for ``purge_keep_days`` (default 10 days). On the dashboard page
System you choose a default for all devices and, per device, a different number of days (1–365):

* shorter: every night at 04:17 (and shortly after a change) Core calls
  ``recorder.purge_entities`` with the device's entities and the chosen days,
* longer: Core raises the recorder's keep time to the longest chosen value (while Home Assistant
  runs, the configured value comes back with a restart and is raised again) and purges all other
  entities down to the default the same way.

Long-term statistics (hourly means and sums, energy dashboard) are not touched.

Stored as ``history`` (``{device_id: days}``), ``history_default`` (days or None = as configured)
and the last run as ``history_run``.
"""

from __future__ import annotations

from collections import defaultdict
import logging
from typing import Any

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.event import async_call_later, async_track_time_change

from .const import SIGNAL_DATA
from .store import CoreStore, now_ms

_LOGGER = logging.getLogger(__name__)

OPTIONS = [1, 2, 3, 5, 7, 14, 30, 60, 90, 180, 365]
MAX_DAYS = 365
RUN_AT = (4, 17)
CHANGE_DELAY_S = 30


def _recorder(hass: HomeAssistant) -> Any:
    try:
        from homeassistant.components.recorder import get_instance  # noqa: PLC0415

        return get_instance(hass)
    except Exception:  # noqa: BLE001 - recorder not loaded
        return None


class HistoryKeeper:
    """History time per device."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self._unsub: list[CALLBACK_TYPE] = []
        self._soon: CALLBACK_TYPE | None = None
        # the recorder's own keep time (from its configuration), before Core raises it
        self.configured: int | None = None

    @property
    def devices(self) -> dict[str, int]:
        """Return the chosen days per device."""
        return self.store.data.setdefault("history", {})

    def default_days(self) -> int | None:
        """Return the default for all devices (own choice, else the recorder's)."""
        own = self.store.data.get("history_default")
        return int(own) if own else self.configured

    def longest(self) -> int | None:
        """Return how long the recorder has to keep states (longest chosen value)."""
        base = self.default_days()
        if base is None:
            return None
        return max([base, *self.devices.values()])

    @callback
    def async_setup(self) -> None:
        """Remember the recorder's keep time, raise it if needed, purge every night."""
        rec = _recorder(self.hass)
        if rec is not None:
            self.configured = int(rec.keep_days)
        self._apply_keep()
        self._unsub.append(
            async_track_time_change(
                self.hass, self._nightly, hour=RUN_AT[0], minute=RUN_AT[1], second=0
            )
        )

    @callback
    def async_stop(self) -> None:
        """Stop and give the recorder its own keep time back."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._soon:
            self._soon()
            self._soon = None
        rec = _recorder(self.hass)
        if rec is not None and self.configured is not None:
            rec.keep_days = self.configured

    @callback
    def _apply_keep(self) -> None:
        """Let the recorder keep states as long as the longest chosen value."""
        rec = _recorder(self.hass)
        if rec is None or self.configured is None:
            return
        rec.keep_days = max(self.configured, self.longest() or 0)

    @callback
    def set_days(self, device_id: str, days: int | None) -> None:
        """Set the days of one device (None or the default: back to the default)."""
        if days is None or days <= 0 or days == self.default_days():
            self.devices.pop(device_id, None)
        else:
            self.devices[device_id] = min(int(days), MAX_DAYS)
        self._changed()

    @callback
    def set_default(self, days: int | None) -> None:
        """Set the default for all devices (None: as configured in the recorder)."""
        if days and days != self.configured:
            self.store.data["history_default"] = min(int(days), MAX_DAYS)
        else:
            self.store.data.pop("history_default", None)
        base = self.default_days()
        for dev, d in list(self.devices.items()):
            if d == base:
                self.devices.pop(dev)
        self._changed()

    @callback
    def _changed(self) -> None:
        self._apply_keep()
        self.store.save(SIGNAL_DATA)
        # several changes in a row: purge once, a little later
        if self._soon:
            self._soon()
        self._soon = async_call_later(self.hass, CHANGE_DELAY_S, self._later)

    @callback
    def _later(self, _now: Any) -> None:
        self._soon = None
        self.hass.async_create_task(self.async_purge())

    @callback
    def _nightly(self, _now: Any) -> None:
        self._apply_keep()
        self.hass.async_create_task(self.async_purge())

    def groups(self) -> dict[int, list[str]]:
        """Return the entities to purge per number of days (only what is below the keep time)."""
        ents = er.async_get(self.hass)
        devs = dr.async_get(self.hass)
        keep = self.longest()
        base = self.default_days()
        out: dict[int, list[str]] = defaultdict(list)
        own: set[str] = set()
        for device_id, days in list(self.devices.items()):
            if devs.async_get(device_id) is None:
                self.devices.pop(device_id, None)  # device removed
                continue
            ids = [e.entity_id for e in er.async_entries_for_device(ents, device_id)]
            own.update(ids)
            if keep is None or days < keep:
                out[days].extend(ids)
        # all other entities down to the default (only if the recorder keeps longer)
        if base is not None and keep is not None and base < keep:
            out[base].extend(e for e in self.hass.states.async_entity_ids() if e not in own)
        return {d: sorted(set(ids)) for d, ids in out.items() if ids}

    async def async_purge(self) -> int:
        """Purge the chosen devices; return the number of entities."""
        if not self.hass.services.has_service("recorder", "purge_entities"):
            return 0
        count = 0
        for days, ids in self.groups().items():
            try:
                await self.hass.services.async_call(
                    "recorder",
                    "purge_entities",
                    {"entity_id": ids, "keep_days": days},
                    blocking=True,
                )
                count += len(ids)
            except Exception:  # noqa: BLE001 - one group must not stop the others
                _LOGGER.warning("History: purging %s entities failed", len(ids), exc_info=True)
        self.store.data["history_run"] = {"at": now_ms(), "entities": count}
        self.store.save(SIGNAL_DATA)
        return count

    def info(self) -> dict[str, Any]:
        """Return the settings for the dashboard."""
        return {
            "devices": dict(self.devices),
            "default": self.default_days(),
            "configured": self.configured,
            "keep": self.longest(),
            "options": OPTIONS,
            "longer": True,
            "last_run": self.store.data.get("history_run"),
        }
