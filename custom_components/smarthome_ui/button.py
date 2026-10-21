"""Buttons: reload a display; skip the robot planner today."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import CoreConfigEntry
from .displays import DisplayEntity, async_setup_display_entities
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
    entry.async_on_unload(
        async_setup_planner_entities(
            hass,
            entry.runtime_data.planners,
            async_add_entities,
            lambda p: [PlannerSkip(entry.entry_id, p)],
        )
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
