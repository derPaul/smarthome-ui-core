"""Security: open windows with rain, everybody left, good night check, last night."""

from __future__ import annotations

from datetime import datetime

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

SOURCES = {"sec.openings": {"push": True}, "sec.night": {"push": True}}


def _local(*args: int) -> datetime:
    return dt_util.as_utc(datetime(*args, tzinfo=dt_util.get_default_time_zone()))


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.store.set_notify({"sources": SOURCES})
    await hass.async_block_till_done()
    return entry


def _house(hass: HomeAssistant) -> None:
    hass.states.async_set(
        "binary_sensor.fenster_bad",
        "on",
        {"device_class": "window", "friendly_name": "Fenster Bad"},
    )
    hass.states.async_set(
        "binary_sensor.haustur", "off", {"device_class": "door", "friendly_name": "Haustür"}
    )
    hass.states.async_set(
        "binary_sensor.kuhlschrank_tur",
        "on",
        {"device_class": "door", "friendly_name": "Kühlschrank Tür"},
    )
    hass.states.async_set("lock.haustur", "unlocked", {"friendly_name": "Haustür Schloss"})
    hass.states.async_set("person.anna", "home", {"friendly_name": "Anna"})
    hass.states.async_set("alarm_control_panel.haus", "disarmed", {})


async def test_rain_leave_good_night(hass: HomeAssistant, notify_calls) -> None:
    _house(hass)
    entry = await _setup(hass)
    security = entry.runtime_data.security
    assert security.open_now() == ["binary_sensor.fenster_bad"]  # not the fridge door
    assert hass.states.get("sensor.security_open").state == "1"

    hass.states.async_set("weather.home", "rainy", {})
    security._check_rain()
    security._check_rain()
    await hass.async_block_till_done()
    assert [c.data["message"] for c in notify_calls] == ["Es regnet – noch offen: Fenster Bad"]

    hass.states.async_set("person.anna", "not_home", {"friendly_name": "Anna"})
    await hass.async_block_till_done()
    assert notify_calls[-1].data["title"] == "Alle weg"
    assert notify_calls[-1].data["message"] == (
        "Noch offen: Fenster Bad · nicht abgeschlossen: Haustür Schloss"
    )

    # good night: off by default, then on with its own time
    hass.states.async_set("person.anna", "home", {"friendly_name": "Anna"})
    await hass.async_block_till_done()
    count = len(notify_calls)
    security._good_night()
    assert len(notify_calls) == count
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.security_good_night_check"}, blocking=True
    )
    await hass.services.async_call(
        "time",
        "set_value",
        {"entity_id": "time.security_good_night", "time": "23:15"},
        blocking=True,
    )
    assert security.checks()["good_night_at"] == "23:15"
    security._good_night()
    await hass.async_block_till_done()
    assert notify_calls[-1].data["title"] == "Gute Nacht"
    assert notify_calls[-1].data["message"].endswith("Alarm nicht scharf")


async def test_last_night(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to(_local(2026, 6, 1, 21, 0))
    _house(hass)
    hass.states.async_set(
        "binary_sensor.einfahrt_person", "off", {"friendly_name": "Einfahrt Person"}
    )
    hass.states.async_set(
        "binary_sensor.flur_bewegung", "off", {"device_class": "motion", "friendly_name": "Flur"}
    )
    entry = await _setup(hass)
    security = entry.runtime_data.security
    sensors = security.night_sensors()
    assert sensors["persons"] == ["binary_sensor.einfahrt_person"]
    assert sensors["motion"] == ["binary_sensor.flur_bewegung"]
    assert "binary_sensor.haustur" in sensors["doors"]

    # before the night: not counted
    hass.states.async_set("binary_sensor.flur_bewegung", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.flur_bewegung", "off", {"device_class": "motion"})

    freezer.move_to(_local(2026, 6, 1, 22, 0))
    security._night_start()
    freezer.move_to(_local(2026, 6, 2, 2, 14))
    hass.states.async_set(
        "binary_sensor.einfahrt_person", "on", {"friendly_name": "Einfahrt Person"}
    )
    hass.states.async_set(
        "binary_sensor.haustur", "on", {"device_class": "door", "friendly_name": "Haustür"}
    )
    hass.states.async_set("binary_sensor.flur_bewegung", "on", {"device_class": "motion"})
    await hass.async_block_till_done()
    summary = security.night_summary()
    assert summary["running"]
    assert summary["counts"]["persons"] == 1
    assert summary["counts"]["doors"] == 1
    assert summary["counts"]["motion"] == 1
    assert hass.states.get("sensor.security_last_night").state == "1"

    freezer.move_to(_local(2026, 6, 2, 6, 0))
    security._night_end()
    security._night_end()
    await hass.async_block_till_done()
    night = [c for c in notify_calls if c.data["title"] == "Letzte Nacht"]
    assert len(night) == 1
    assert night[0].data["message"] == (
        "1 Person erkannt (Einfahrt Person 02:14) · 1× Tür geöffnet (Haustür 02:14)"
        " · 1× Bewegung im Haus"
    )
    # during the day the last night stays visible
    freezer.move_to(_local(2026, 6, 2, 12, 0))
    assert security.night_summary()["counts"]["persons"] == 1
    assert not security.night_summary()["running"]


async def test_settings_from_dashboard(hass: HomeAssistant, hass_ws_client) -> None:
    _house(hass)
    await _setup(hass)
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "smarthome_ui/security/settings",
            "openings": ["binary_sensor.haustur"],
            "night": {"start": "23:00", "end": "07:00"},
        }
    )
    res = await client.receive_json()
    assert res["success"]
    assert res["result"]["open"] == []  # only the front door counts now, and it is closed
    assert res["result"]["checks"]["rain"] is True
