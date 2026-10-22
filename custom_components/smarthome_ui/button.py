"""Buttons: reload a display; skip the robot planner today; waste put out; appliance emptied."""

from __future__ import annotations

from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import CoreConfigEntry
from .appliance import ApplianceEntity, ApplianceTracker, async_setup_appliance_entities
from .displays import DisplayEntity, async_setup_display_entities
from .entity import DEVICE_HOUSEHOLD, CoreEntity
from .planner import PlannerEntity, RobotPlanner, async_setup_planner_entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one reload button per display."""
    manager = entry.runtime_data.displays
    entry.async_on_unload(
        async_setup_display_entities(
            hass,
            entry.entry_id,
            manager,
            async_add_entities,
            lambda did: [DisplayReload(entry.entry_id, manager, did, "reload", "button")],
        )
    )
    async_add_entities([WastePutOut(entry.entry_id, entry.runtime_data)])
    entry.async_on_unload(
        async_setup_planner_entities(
            hass,
            entry.runtime_data.planners,
            async_add_entities,
            lambda p: [PlannerSkip(entry.entry_id, p)],
        )
    )
    async_setup_appliance_entities(
        entry.runtime_data.appliances,
        async_add_entities,
        lambda t: [ApplianceUnloaded(entry.entry_id, t)] if t.unloads else [],
    )


class DisplayReload(DisplayEntity, ButtonEntity):
    """Reload the dashboard on this display."""

    async def async_press(self) -> None:
        """Send the reload command."""
        self._command({"type": "reload"})


class PlannerSkip(PlannerEntity, ButtonEntity):
    """No automatic start today."""

    _attr_translation_key = "planner_skip"

    def __init__(self, entry_id: str, planner: RobotPlanner) -> None:
        """Initialize (button.garden_<mower>_skip_today / button.household_<vacuum>_skip_today)."""
        super().__init__(
            entry_id, planner, "_skip", "button", f"{slugify(planner.tracker.name)}_skip_today"
        )

    async def async_press(self) -> None:
        """Skip today."""
        self._planner.skip_today()


class WastePutOut(CoreEntity, ButtonEntity):
    """The waste is put out (for the pickup whose reminder runs, else the next one)."""

    _attr_translation_key = "waste_put_out"

    def __init__(self, entry_id: str, runtime: Any) -> None:
        """Initialize (button.household_waste_put_out)."""
        super().__init__(entry_id, "waste_put_out", DEVICE_HOUSEHOLD, "button", "waste_put_out")
        self._runtime = runtime

    async def async_press(self) -> None:
        """Mark the pickup as put out."""
        waste = self._runtime.waste
        entry = waste.current() or (waste.days[0] if waste.days else None)
        if entry is not None:
            self._runtime.store.mark_done(waste.key(entry["date"]), label="Müll rausgestellt")


class ApplianceUnloaded(ApplianceEntity, ButtonEntity):
    """The appliance has been emptied."""

    def __init__(self, entry_id: str, tracker: ApplianceTracker) -> None:
        """Initialize (button.household_<appliance>_unloaded)."""
        super().__init__(entry_id, tracker, "unloaded", "button")

    async def async_press(self) -> None:
        """Mark as emptied."""
        user = (
            await self.hass.auth.async_get_user(self._context.user_id)
            if self._context and self._context.user_id
            else None
        )
        self._tracker.unloaded(user.name if user else None)
