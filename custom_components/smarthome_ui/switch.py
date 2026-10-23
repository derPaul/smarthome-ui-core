"""Switches: pause all notifications (e.g. on holiday) and the standby screen of each display."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import CoreConfigEntry
from .alarm import OPTION_SWITCHES, AlarmEntity, AlarmOption
from .const import SIGNAL_NOTIFY, SIGNAL_PRESENCE_SIM, SIGNAL_SECURITY
from .displays import DisplayEntity, async_setup_display_entities
from .doorbell import DoorbellEntity, DoorbellTracker, async_setup_doorbell_entities
from .entity import DEVICE_NOTIFY, DEVICE_SECURITY, CoreEntity
from .planner import PlannerEntity, RobotPlanner, async_setup_planner_entities
from .store import CoreStore
from .system import OPTIONS as SYSTEM_OPTIONS, SystemOption
from .technik import TechnikOption


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the notifications switch and one standby switch per display."""
    async_add_entities([NotificationsSwitch(entry.entry_id, entry.runtime_data.store)])
    security = entry.runtime_data.security
    async_add_entities(
        SecurityCheck(entry.entry_id, security, check, key)
        for check, key in (
            ("rain", "rain_warning"),
            ("leave", "leave_check"),
            ("good_night", "good_night_check"),
            ("open_long", "open_long_check"),
        )
    )
    alarm = entry.runtime_data.alarm
    async_add_entities(
        [AlarmLogicSwitch(entry.entry_id, alarm), GuestsSwitch(entry.entry_id, alarm)]
        + [AlarmOptionSwitch(entry.entry_id, alarm, *option) for option in OPTION_SWITCHES]
        + [PresenceSimSwitch(entry.entry_id, entry.runtime_data.presence_sim)]
        + [
            TechnikCheck(entry.entry_id, entry.runtime_data.technik, check)
            for check in ("pressure", "water", "cycling", "baseload")
        ]
        + [SystemOption(entry.entry_id, entry.runtime_data.system, key) for key in SYSTEM_OPTIONS]
    )
    async_setup_doorbell_entities(
        entry.runtime_data.doorbells,
        async_add_entities,
        lambda t: [DoorbellNotify(entry.entry_id, t)],
    )
    manager = entry.runtime_data.displays
    entry.async_on_unload(
        async_setup_display_entities(
            hass,
            entry.entry_id,
            manager,
            async_add_entities,
            lambda did: [DisplayStandby(entry.entry_id, manager, did, "standby", "switch")],
        )
    )
    entry.async_on_unload(
        async_setup_planner_entities(
            hass,
            entry.runtime_data.planners,
            async_add_entities,
            lambda p: [PlannerSwitch(entry.entry_id, p)],
            lambda p, area: PlannerAreaSwitch(entry.entry_id, p, area),
        )
    )
    entry.async_on_unload(
        async_setup_planner_entities(
            hass,
            entry.runtime_data.planners,
            async_add_entities,
            lambda p: [],
            lambda p, area: PlannerMopSwitch(entry.entry_id, p, area) if p.can_mop() else None,
        )
    )


class NotificationsSwitch(CoreEntity, SwitchEntity):
    """Notifications on or paused."""

    _attr_name = None  # main entity of the device: switch.<device name>
    _attr_translation_key = "notifications"  # icon

    def __init__(self, entry_id: str, store: CoreStore) -> None:
        """Initialize the switch."""
        super().__init__(entry_id, "notifications", DEVICE_NOTIFY, "switch")
        self._store = store

    @property
    def is_on(self) -> bool:
        """Return True if notifications are sent."""
        return bool(self._store.data["notify"].get("enabled", True))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Resume notifications."""
        self._store.set_notify({"enabled": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Pause notifications."""
        self._store.set_notify({"enabled": False})

    async def async_added_to_hass(self) -> None:
        """Update when the setting changes."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_NOTIFY, self.async_write_ha_state)
        )


class DisplayStandby(DisplayEntity, SwitchEntity):
    """Standby screen (clock, weather) of a display; turns off when someone touches it."""

    @property
    def is_on(self) -> bool:
        """Return True while the standby screen is shown."""
        return bool(self._record["state"].get("standby"))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Show the standby screen."""
        self._command({"type": "set", "key": "standby", "value": True})

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Wake the display."""
        self._command({"type": "set", "key": "standby", "value": False})


class PlannerSwitch(PlannerEntity, SwitchEntity):
    """Mow / vacuum automatically when areas are due (off by default)."""

    def __init__(self, entry_id: str, planner: RobotPlanner) -> None:
        """Initialize (switch.garden_<mower>_auto / switch.household_<vacuum>_auto)."""
        super().__init__(entry_id, planner, "", "switch", f"{slugify(planner.tracker.name)}_auto")
        self._attr_translation_key = f"planner_{planner.kind}"

    @property
    def is_on(self) -> bool:
        """Return True if the robot may start on its own."""
        return self._planner.is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Allow automatic starts."""
        self._planner.set_on(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """No automatic starts."""
        self._planner.set_on(False)


class PlannerAreaSwitch(PlannerEntity, SwitchEntity):
    """One area / room may be done automatically."""

    _attr_translation_key = "planner_area"

    def __init__(self, entry_id: str, planner: RobotPlanner, area: str) -> None:
        """Initialize (switch.garden_<area>_auto / switch.household_<room>_auto)."""
        name = planner.area_name(area)
        super().__init__(entry_id, planner, f"_{slugify(area)}", "switch", f"{slugify(name)}_auto")
        self._area = area
        self._attr_translation_placeholders = {"area": name}

    @property
    def is_on(self) -> bool:
        """Return True if the area is included."""
        return self._planner.area_on(self._area)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Include the area."""
        self._planner.set_area(self._area, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Leave the area out."""
        self._planner.set_area(self._area, False)


class PlannerMopSwitch(PlannerEntity, SwitchEntity):
    """A room may be mopped automatically (switch off e.g. for carpets)."""

    _attr_translation_key = "planner_mop"

    def __init__(self, entry_id: str, planner: RobotPlanner, area: str) -> None:
        """Initialize (switch.household_<room>_mop)."""
        name = planner.area_name(area)
        super().__init__(
            entry_id, planner, f"_{slugify(area)}_mop", "switch", f"{slugify(name)}_mop"
        )
        self._area = area
        self._attr_translation_placeholders = {"area": name}

    @property
    def is_on(self) -> bool:
        """Return True if the room is mopped."""
        return self._planner.mop_on(self._area)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Mop the room."""
        self._planner.set_mop(self._area, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Do not mop the room."""
        self._planner.set_mop(self._area, False)


class SecurityCheck(CoreEntity, SwitchEntity):
    """A check of the device "Sicherheit" on or off (rain, everybody left, good night)."""

    def __init__(self, entry_id: str, security: Any, check: str, key: str) -> None:
        """Initialize (switch.security_<key>)."""
        super().__init__(entry_id, f"security_{key}", DEVICE_SECURITY, "switch", key)
        self._security = security
        self._check = check
        self._attr_translation_key = f"security_{key}"

    @property
    def is_on(self) -> bool:
        """Return True if the check is on."""
        return bool(self._security.checks()[self._check])

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the check on."""
        self._security.set_check(self._check, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the check off."""
        self._security.set_check(self._check, False)

    async def async_added_to_hass(self) -> None:
        """Update when the settings change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SECURITY, self.async_write_ha_state)
        )


class DoorbellNotify(DoorbellEntity, SwitchEntity):
    """Ring notification of the doorbell on or off (e.g. while the baby sleeps)."""

    def __init__(self, entry_id: str, tracker: DoorbellTracker) -> None:
        """Initialize (switch.security_<doorbell>_notify)."""
        super().__init__(entry_id, tracker, "notify", "switch")

    @property
    def is_on(self) -> bool:
        """Return True if a ring is notified."""
        return not self._tracker.muted

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Notify rings again."""
        self._tracker.set_muted(False)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """No notification when it rings."""
        self._tracker.set_muted(True)


class AlarmLogicSwitch(AlarmEntity, SwitchEntity):
    """Core runs the alarm logic (off: Core triggers and reacts to nothing)."""

    def __init__(self, entry_id: str, alarm: Any) -> None:
        """Initialize (switch.security_alarm_logic)."""
        super().__init__(entry_id, alarm, "alarm_logic", "switch")

    @property
    def is_on(self) -> bool:
        """Return True if Core runs the alarm logic."""
        return self._alarm.enabled

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Run the alarm logic."""
        self._alarm.set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop the alarm logic."""
        self._alarm.set_enabled(False)


class GuestsSwitch(AlarmEntity, SwitchEntity):
    """Guests are there: the chosen motion sensors are silent in the guest modes."""

    def __init__(self, entry_id: str, alarm: Any) -> None:
        """Initialize (switch.security_guests)."""
        super().__init__(entry_id, alarm, "guests", "switch")

    @property
    def is_on(self) -> bool:
        """Return True while guests are there."""
        return self._alarm.guests_on

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the guest devices at home."""
        return {"devices": self._alarm.guests_present() if self._alarm.configured else []}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Guests are there."""
        self._alarm.set_guests(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """No guests."""
        self._alarm.set_guests(False)


class AlarmOptionSwitch(AlarmOption, SwitchEntity):
    """An alarm setting on / off: night walk, critical push, push at entry delay / end, lights."""

    def __init__(self, entry_id: str, alarm: Any, section: str, key: str, name: str) -> None:
        """Initialize (switch.security_<name>)."""
        super().__init__(entry_id, alarm, section, key, name, "switch")

    @property
    def is_on(self) -> bool:
        """Return True if the setting is on."""
        return bool(self._value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the setting on."""
        self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the setting off."""
        self._set(False)


class PresenceSimSwitch(CoreEntity, SwitchEntity):
    """Simulate presence: lights in the evening, covers at dusk and dawn."""

    _attr_translation_key = "security_presence_sim"

    def __init__(self, entry_id: str, sim: Any) -> None:
        """Initialize (switch.security_presence_sim)."""
        super().__init__(
            entry_id, "security_presence_sim", DEVICE_SECURITY, "switch", "presence_sim"
        )
        self._sim = sim

    @property
    def available(self) -> bool:
        """Return True once lights or covers are chosen (in the dashboard)."""
        return self._sim.configured

    @property
    def is_on(self) -> bool:
        """Return True if the simulation is on."""
        return self._sim.enabled

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return what it does right now (off / waiting / ready / running)."""
        return {"state": self._sim.state(), "lights_on": list(self._sim.info()["lights_on"])}

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the simulation on."""
        self._sim.set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the simulation off (lights it switched on go off)."""
        self._sim.set_enabled(False)

    async def async_added_to_hass(self) -> None:
        """Update when the settings change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PRESENCE_SIM, self.async_write_ha_state)
        )


class TechnikCheck(TechnikOption, SwitchEntity):
    """A house technology check on / off (pressure, water watch, cycling, base load)."""

    def __init__(self, entry_id: str, technik: Any, check: str) -> None:
        """Initialize (switch.technik_<check>_watch)."""
        super().__init__(entry_id, technik, f"{check}_watch", "switch")
        self._check = check

    @property
    def is_on(self) -> bool:
        """Return True if the check is on."""
        return bool(self._technik.settings()["checks"][self._check])

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Switch the check on."""
        self._technik.set_option(self._check, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Switch the check off."""
        self._technik.set_option(self._check, False)
