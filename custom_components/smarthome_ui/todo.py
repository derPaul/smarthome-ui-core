"""To-do lists (household, garden) with the tasks that are due; checking one marks it done."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoItemStatus,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.util import dt as dt_util

from . import CoreConfigEntry
from .const import DOMAIN, SIGNAL_TASKS
from .entity import DEVICE_GARDEN, DEVICE_HOUSEHOLD, CoreEntity, device_for_source
from .store import CoreStore
from .tasks import due_today, task_states


async def async_setup_entry(
    hass: HomeAssistant,
    entry: CoreConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the to-do list."""
    store = entry.runtime_data.store
    async_add_entities(
        DueTodoList(entry.entry_id, store, device) for device in (DEVICE_HOUSEHOLD, DEVICE_GARDEN)
    )


class DueTodoList(CoreEntity, TodoListEntity):
    """The due tasks of one area (household or garden)."""

    _attr_name = None  # main entity of the device: todo.<device name>
    _attr_translation_key = "due"  # icon
    _attr_supported_features = TodoListEntityFeature.UPDATE_TODO_ITEM

    def __init__(self, entry_id: str, store: CoreStore, device: str) -> None:
        """Initialize the list."""
        super().__init__(entry_id, f"due_{device}", device)
        self._store = store
        self._device = device

    def _states(self) -> list:
        return [
            t
            for t in task_states(self._store.data["tasks"], self._store.done)
            if device_for_source(t.src) == self._device
        ]

    @property
    def todo_items(self) -> list[TodoItem]:
        """Return the tasks due today or overdue, oldest first."""
        today = dt_util.now().date()
        due = [t for t in self._states() if due_today(t, today)]
        due.sort(key=lambda t: (t.due or 0, t.name))
        return [
            TodoItem(
                uid=task.key,
                summary=task.name,
                status=TodoItemStatus.NEEDS_ACTION,
                due=task.due_date,
            )
            for task in due
        ]

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Mark a task done when it is checked."""
        if item.status != TodoItemStatus.COMPLETED:
            return
        task = next((t for t in self._states() if t.key == item.uid), None)
        if task is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_task",
                translation_placeholders={"task": item.summary or item.uid or ""},
            )
        user = (
            await self.hass.auth.async_get_user(self._context.user_id)
            if self._context and self._context.user_id
            else None
        )
        self._store.mark_done(task.key, who=user.name if user else None, label=task.name)

    async def async_added_to_hass(self) -> None:
        """Update when tasks or done records change."""
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_TASKS, self.async_write_ha_state)
        )
