"""Task sensors, the to-do list and completion."""

from __future__ import annotations

from datetime import datetime

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.todo import DOMAIN as TODO_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

DAY = 86_400_000


def _ms(text: str) -> int:
    return int(
        dt_util.as_utc(
            datetime.fromisoformat(text).replace(tzinfo=dt_util.get_default_time_zone())
        ).timestamp()
        * 1000
    )


async def test_task_sensors_and_todo(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    freezer.move_to("2026-10-21 10:00:00")
    store = entry.runtime_data.store
    store.set_tasks(
        [
            {
                "key": "clean:bad",
                "name": "Bad putzen",
                "src": "hh.tasks",
                "area": "bad",
                "interval": 7,
                "last": _ms("2026-10-12 09:00"),
                "due": _ms("2026-10-19 09:00"),
            },
            {
                "key": "hh:task:filter|",
                "name": "Filter reinigen",
                "src": "hh.tasks",
                "interval": 30,
                "last": _ms("2026-10-10 09:00"),
                "due": _ms("2026-11-09 09:00"),
            },
        ]
    )
    await hass.async_block_till_done()

    bad = hass.states.get("sensor.smarthome_ui_bad_putzen")
    assert bad is not None
    assert dt_util.parse_datetime(bad.state).date().isoformat() == "2026-10-19"
    assert bad.attributes["days_left"] == -2
    assert bad.attributes["due"] is True
    assert hass.states.get("sensor.smarthome_ui_filter_reinigen").attributes["due"] is False

    items = await hass.services.async_call(
        TODO_DOMAIN,
        "get_items",
        {},
        target={"entity_id": "todo.smarthome_ui_due_tasks"},
        blocking=True,
        return_response=True,
    )
    summaries = [i["summary"] for i in items["todo.smarthome_ui_due_tasks"]["items"]]
    assert summaries == ["Bad putzen"]

    # check it off in the to-do list → done now, next due in 7 days
    await hass.services.async_call(
        TODO_DOMAIN,
        "update_item",
        {"item": "Bad putzen", "status": "completed"},
        target={"entity_id": "todo.smarthome_ui_due_tasks"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert store.done["clean:bad"]["t"] > 0
    bad = hass.states.get("sensor.smarthome_ui_bad_putzen")
    assert dt_util.parse_datetime(bad.state).date().isoformat() == "2026-10-28"
    assert hass.states.get("todo.smarthome_ui_due_tasks").state == "0"

    # removed from the dashboard → sensor removed
    store.set_tasks([{"key": "clean:bad", "name": "Bad putzen", "interval": 7}])
    await hass.async_block_till_done()
    assert hass.states.get("sensor.smarthome_ui_filter_reinigen") is None
