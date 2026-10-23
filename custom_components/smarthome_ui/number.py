"""Numbers: cleaning interval per room (days) on the household device; alarm times."""

from __future__ import annotations

from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import slugify

from . import CoreConfigEntry
from .alarm import OPTION_NUMBERS, AlarmLogic, AlarmOption
from .const import SIGNAL_SECURITY, SIGNAL_TASKS
from .entity import DEVICE_HOUSEHOLD, DEVICE_SECURITY, CoreEntity
from .store import CoreStore
from .technik import TechnikOption


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add one interval per room the dashboard tracks for cleaning."""
    store = entry.runtime_data.store
    known: set[str] = set()

    @callback
    def sync() -> None:
        new = []
        for task in store.data["tasks"]:
            key = str(task.get("key", ""))
            if not key.startswith("clean:") or key in known:
                continue
            known.add(key)
            new.append(CleanInterval(entry.entry_id, store, key.removeprefix("clean:"), task))
        if new:
            async_add_entities(new)

    sync()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_TASKS, sync))
    async_add_entities([OpenMinutes(entry.entry_id, entry.runtime_data.security)])
    technik = entry.runtime_data.technik
    async_add_entities(
        [
            TechnikNumber(entry.entry_id, technik, "pressure_min", 0.5, 3, 0.1, "bar"),
            TechnikNumber(entry.entry_id, technik, "water_minutes", 5, 240, 5, UnitOfTime.MINUTES),
        ]
    )
    alarm = entry.runtime_data.alarm
    async_add_entities(AlarmOptionNumber(entry.entry_id, alarm, *o) for o in OPTION_NUMBERS)


class CleanInterval(CoreEntity, NumberEntity):
    """How often a room should be cleaned (days, 0 = not tracked)."""

    _attr_translation_key = "clean_interval"
    _attr_native_min_value = 0
    _attr_native_max_value = 90
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTime.DAYS

    def __init__(self, entry_id: str, store: CoreStore, area_id: str, task: dict) -> None:
        """Initialize (entity ID: number.household_<room>_clean_interval)."""
        room = str(task.get("area") or area_id)
        super().__init__(
            entry_id,
            f"clean_interval_{area_id}",
            DEVICE_HOUSEHOLD,
            "number",
            f"{slugify(room) or area_id}_clean_interval",
        )
        self._store = store
        self._area = area_id
        self._attr_translation_placeholders = {"area": room}

    @property
    def native_value(self) -> float | None:
        """Return the own interval, else the one the dashboard uses."""
        own = self._store.data["cleaning"]["intervals"].get(self._area)
        if own is not None:
            return own
        task = next(
            (t for t in self._store.data["tasks"] if t.get("key") == f"clean:{self._area}"), None
        )
        return task.get("interval") if task else None

    async def async_set_native_value(self, value: float) -> None:
        """Set the interval (the dashboard follows)."""
        intervals = dict(self._store.data["cleaning"]["intervals"])
        intervals[self._area] = int(value)
        self._store.set_cleaning_intervals(intervals)

    async def async_added_to_hass(self) -> None:
        """Update when tasks or settings change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_TASKS, self.async_write_ha_state)
        )


class OpenMinutes(CoreEntity, NumberEntity):
    """After how many minutes an open window or door is reported."""

    _attr_translation_key = "security_open_minutes"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_min_value = 1
    _attr_native_max_value = 120
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES

    def __init__(self, entry_id: str, security: Any) -> None:
        """Initialize (number.security_open_minutes)."""
        super().__init__(
            entry_id, "security_open_minutes", DEVICE_SECURITY, "number", "open_minutes"
        )
        self._security = security

    @property
    def native_value(self) -> float | None:
        """Return the minutes."""
        return float(self._security.checks()["open_minutes"])

    async def async_set_native_value(self, value: float) -> None:
        """Set the minutes."""
        self._security.set_check("open_minutes", int(value))

    async def async_added_to_hass(self) -> None:
        """Update when the settings change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_SECURITY, self.async_write_ha_state)
        )


class AlarmOptionNumber(AlarmOption, NumberEntity):
    """An alarm time: night walk window / quiet time / question delay, guest away time."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        entry_id: str,
        alarm: AlarmLogic,
        section: str,
        key: str,
        name: str,
        low: float,
        high: float,
        step: float,
        unit: str,
    ) -> None:
        """Initialize (number.security_<name>)."""
        super().__init__(entry_id, alarm, section, key, name, "number")
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        """Return the setting."""
        value = self._value
        return float(value) if value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Change the setting."""
        self._set(int(value) if value == int(value) else value)


class TechnikNumber(TechnikOption, NumberEntity):
    """A threshold: heating pressure minimum (bar), minutes of flowing water."""

    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        entry_id: str,
        technik: Any,
        key: str,
        low: float,
        high: float,
        step: float,
        unit: str,
    ) -> None:
        """Initialize (number.technik_<key>)."""
        super().__init__(entry_id, technik, key, "number")
        self._key = key
        self._attr_native_min_value = low
        self._attr_native_max_value = high
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        """Return the threshold."""
        return float(self._technik.settings()[self._key])

    async def async_set_native_value(self, value: float) -> None:
        """Set the threshold (the dashboard does not overwrite it then)."""
        self._technik.set_option(self._key, value)
