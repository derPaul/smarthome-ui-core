"""Task sensors, the to-do list and completion."""

from __future__ import annotations

from datetime import datetime

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.todo import DOMAIN as TODO_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
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

    bad = hass.states.get("sensor.household_bad_putzen")
    assert bad is not None
    assert dt_util.parse_datetime(bad.state).date().isoformat() == "2026-10-19"
    assert bad.attributes["days_left"] == -2
    assert bad.attributes["due"] is True
    assert hass.states.get("sensor.household_filter_reinigen").attributes["due"] is False

    items = await hass.services.async_call(
        TODO_DOMAIN,
        "get_items",
        {},
        target={"entity_id": "todo.household"},
        blocking=True,
        return_response=True,
    )
    summaries = [i["summary"] for i in items["todo.household"]["items"]]
    assert summaries == ["Bad putzen"]

    # check it off in the to-do list → done now, next due in 7 days
    await hass.services.async_call(
        TODO_DOMAIN,
        "update_item",
        {"item": "Bad putzen", "status": "completed"},
        target={"entity_id": "todo.household"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert store.done["clean:bad"]["t"] > 0
    bad = hass.states.get("sensor.household_bad_putzen")
    assert dt_util.parse_datetime(bad.state).date().isoformat() == "2026-10-28"
    assert hass.states.get("todo.household").state == "0"

    # removed from the dashboard → sensor removed
    store.set_tasks([{"key": "clean:bad", "name": "Bad putzen", "interval": 7}])
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_filter_reinigen") is None


async def test_devices_per_area(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    freezer.move_to("2026-10-21 10:00:00")
    entry.runtime_data.store.set_tasks(
        [
            {
                "key": "clean:bad",
                "name": "Bad putzen",
                "src": "hh.tasks",
                "interval": 7,
                "due": _ms("2026-10-20 09:00"),
            },
            {
                "key": "gd:task:hecke|",
                "name": "Hecke schneiden",
                "src": "gd.tasks",
                "due": _ms("2026-10-21 08:00"),
            },
        ]
    )
    await hass.async_block_till_done()

    devices = dr.async_get(hass)
    entities = er.async_get(hass)
    names = sorted(d.name for d in dr.async_entries_for_config_entry(devices, entry.entry_id))
    assert names == ["Garden", "Household", "Notifications", "Security"]

    def device_of(entity_id: str) -> str:
        return devices.async_get(entities.async_get(entity_id).device_id).name

    assert device_of("sensor.household_bad_putzen") == "Household"
    assert device_of("sensor.garden_hecke_schneiden") == "Garden"
    assert device_of("switch.notifications") == "Notifications"
    assert device_of("sensor.notifications_last_sent") == "Notifications"
    assert hass.states.get("todo.household").state == "1"
    assert hass.states.get("todo.garden").state == "1"


async def test_english_entity_ids_with_german_names(hass: HomeAssistant, notify_calls) -> None:
    await hass.config.async_update(language="de")
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.store.set_tasks(
        [{"key": "gd:task:hecke|", "name": "Hecke schneiden", "src": "gd.tasks"}]
    )
    await hass.async_block_till_done()

    ids = sorted(
        e.entity_id for e in er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    )
    assert ids == [
        "binary_sensor.household_mail",
        "binary_sensor.security_all_asleep",
        "button.household_mail_emptied",
        "button.household_waste_put_out",
        "button.security_nightwalk_open",
        "number.security_guests_away",
        "number.security_nightwalk_ask_delay",
        "number.security_nightwalk_quiet",
        "number.security_nightwalk_window",
        "number.security_open_minutes",
        "select.security_nightwalk_effect",
        "sensor.garden_hecke_schneiden",
        "sensor.household_waste_next",
        "sensor.notifications_last_sent",
        "sensor.security_alarm_reason",
        "sensor.security_last_night",
        "sensor.security_nightwalk_until",
        "sensor.security_open",
        "sensor.security_persons_home",
        "switch.notifications",
        "switch.security_alarm_clear",
        "switch.security_alarm_critical",
        "switch.security_alarm_lights",
        "switch.security_alarm_logic",
        "switch.security_alarm_pending",
        "switch.security_good_night_check",
        "switch.security_guests",
        "switch.security_guests_push",
        "switch.security_leave_check",
        "switch.security_nightwalk",
        "switch.security_open_long_check",
        "switch.security_presence_sim",
        "switch.security_rain_warning",
        "time.security_good_night",
        "todo.garden",
        "todo.household",
    ]
    assert hass.states.get("todo.garden").attributes["friendly_name"] == "Garten"
    assert hass.states.get("switch.notifications").attributes["friendly_name"] == "Mitteilungen"
