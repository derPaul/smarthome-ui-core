"""The energy dashboard of Home Assistant as source for the dashboard's energy page.

The meters (grid import and export, solar, battery charge and discharge, water, gas), the power
sensors (grid, solar, battery) and the individual devices are already set up in the energy
dashboard. Core reads them from there, so a new dashboard shows history, records, costs and
consumers without any setting of its own. Kept up to date when the energy dashboard changes.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant

from .const import SIGNAL_DATA
from .store import CoreStore

_LOGGER = logging.getLogger(__name__)


def _ids(values: list[Any]) -> list[str]:
    """Return the statistic ids that are entities (no external statistics)."""
    return [v for v in values if isinstance(v, str) and "." in v and ":" not in v]


def energy_from_prefs(prefs: dict[str, Any]) -> dict[str, Any]:
    """Return the meters of the energy dashboard's preferences."""
    out: dict[str, Any] = {
        "import": [],
        "export": [],
        "pv": [],
        "battery_charge": [],
        "battery_discharge": [],
        "water": [],
        "gas": [],
        "grid_power": [],
        "pv_power": [],
        "battery_power": [],
        "consumers": [],
    }
    for src in prefs.get("energy_sources") or []:
        kind = src.get("type")
        if kind == "grid":
            # older format: lists flow_from / flow_to; newer: one meter per source
            out["import"] += _ids([f.get("stat_energy_from") for f in src.get("flow_from") or []])
            out["export"] += _ids([f.get("stat_energy_to") for f in src.get("flow_to") or []])
            out["import"] += _ids([src.get("stat_energy_from")])
            out["export"] += _ids([src.get("stat_energy_to")])
            out["grid_power"] += _ids([p.get("stat_rate") for p in src.get("power") or []])
            out["grid_power"] += _ids([src.get("stat_rate")])
        elif kind == "solar":
            out["pv"] += _ids([src.get("stat_energy_from")])
            out["pv_power"] += _ids([src.get("stat_rate")])
        elif kind == "battery":
            out["battery_discharge"] += _ids([src.get("stat_energy_from")])
            out["battery_charge"] += _ids([src.get("stat_energy_to")])
            out["battery_power"] += _ids([src.get("stat_rate")])
        elif kind in ("water", "gas"):
            out[kind] += _ids([src.get("stat_energy_from")])
    for dev in prefs.get("device_consumption") or []:
        if eid := next(iter(_ids([dev.get("stat_consumption")])), None):
            out["consumers"].append({"entity": eid, "name": dev.get("name") or None})
    return out


class EnergyPrefs:
    """The meters of the energy dashboard (read at start and whenever it changes)."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store
        self.data: dict[str, Any] = energy_from_prefs({})

    async def async_setup(self) -> None:
        """Read the preferences and follow their changes."""
        try:
            from homeassistant.components.energy.data import async_get_manager

            manager = await async_get_manager(self.hass)
        except Exception as err:  # noqa: BLE001 - the energy dashboard is optional
            _LOGGER.debug("Energy dashboard not available: %s", err)
            return
        self.data = energy_from_prefs(manager.data or {})
        # one listener per Home Assistant run, it always reaches the current entry (after a reload)
        holder = self.hass.data.setdefault("smarthome_ui_energy", {})
        holder["current"] = self
        if not holder.get("listening"):
            holder["listening"] = True

            async def updated() -> None:
                if (current := holder.get("current")) is not None:
                    await current.async_updated()

            manager.async_listen_updates(updated)

    async def async_updated(self) -> None:
        """Read the preferences again (the energy dashboard changed)."""
        await self.async_setup()
        self.store.save(SIGNAL_DATA)

    def info(self) -> dict[str, Any]:
        """Return the meters for the dashboard."""
        return self.data
