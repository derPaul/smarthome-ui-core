"""History per device: keep the recorder history of single devices shorter than the default.

The recorder keeps every state for ``purge_keep_days`` (default 10 days). Busy devices (power
meters, network devices, robots) fill the database most. Per device the dashboard (page System)
can set a shorter time: every night at 04:17 Core calls ``recorder.purge_entities`` with the
device's entities and the chosen days, right after a change as well.

Long-term statistics (hourly means and sums, energy dashboard) are not touched. Longer than the
default is not possible this way: the recorder deletes older states of all entities anyway.

Stored as ``history``: ``{device_id: days}``; the last run as ``history_run``.
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

OPTIONS = [1, 2, 3, 5, 7, 14, 30]
RUN_AT = (4, 17)
CHANGE_DELAY_S = 30


class HistoryKeeper:
    """Shorter history for chosen devices."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self._unsub: list[CALLBACK_TYPE] = []
        self._soon: CALLBACK_TYPE | None = None

    @property
    def devices(self) -> dict[str, int]:
        """Return the chosen days per device."""
        return self.store.data.setdefault("history", {})

    def default_days(self) -> int | None:
        """Return how long the recorder keeps states (None without recorder)."""
        try:
            from homeassistant.components.recorder import get_instance  # noqa: PLC0415

            return int(get_instance(self.hass).keep_days)
        except Exception:  # noqa: BLE001 - recorder not loaded
            return None

    @callback
    def async_setup(self) -> None:
        """Purge every night."""
        self._unsub.append(
            async_track_time_change(
                self.hass, self._nightly, hour=RUN_AT[0], minute=RUN_AT[1], second=0
            )
        )

    @callback
    def async_stop(self) -> None:
        """Stop."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()
        if self._soon:
            self._soon()
            self._soon = None

    @callback
    def set_days(self, device_id: str, days: int | None) -> None:
        """Set the days of one device (None or the default: back to the default)."""
        default = self.default_days()
        if days is None or days <= 0 or (default is not None and days >= default):
            self.devices.pop(device_id, None)
        else:
            self.devices[device_id] = int(days)
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
        self.hass.async_create_task(self.async_purge())

    def groups(self) -> dict[int, list[str]]:
        """Return the entities to purge per number of days."""
        ents = er.async_get(self.hass)
        devs = dr.async_get(self.hass)
        out: dict[int, list[str]] = defaultdict(list)
        for device_id, days in list(self.devices.items()):
            if devs.async_get(device_id) is None:
                self.devices.pop(device_id, None)  # device removed
                continue
            out[days].extend(e.entity_id for e in er.async_entries_for_device(ents, device_id))
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
            "options": OPTIONS,
            "last_run": self.store.data.get("history_run"),
        }
