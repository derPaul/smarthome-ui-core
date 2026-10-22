"""Alarm logic: rules per mode, guests, night walk (allow / ask), reaction, switch off."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import HomeAssistant

PANEL = "alarm_control_panel.haus"
WINDOW = "binary_sensor.fenster_wohnzimmer"
HALL = "binary_sensor.flur_bewegung"
UP = "binary_sensor.og_bewegung"


def _states(hass: HomeAssistant, mode: str) -> None:
    hass.states.async_set(PANEL, mode, {"supported_features": 7})
    hass.states.async_set(WINDOW, "off", {"device_class": "window", "friendly_name": "Fenster WZ"})
    hass.states.async_set(HALL, "off", {"device_class": "motion", "friendly_name": "Flur"})
    hass.states.async_set(UP, "off", {"device_class": "motion", "friendly_name": "OG"})


SETTINGS = {
    "panel": PANEL,
    "rules": {
        WINDOW: ["armed_home", "armed_away", "armed_night"],
        HALL: ["armed_away", "armed_night", "armed_home"],
    },
    "actions": {
        "targets": ["mobile_app_phone"],
        "critical": True,
        "pending": True,
        "clear": True,
        "sirens": ["siren.sirene"],
        "lights": False,
    },
    "guests": {
        "devices": ["device_tracker.gast"],
        "ssids": [],
        "unifi_guest": False,
        "away": 30,
        "modes": ["armed_home"],
        "push_arm": True,
        "skip": [HALL],
    },
    "nightwalk": {
        "enabled": False,
        "starts": [UP],
        "affected": [HALL],
        "below": [HALL, WINDOW],
        "window": 3,
        "quiet": 5,
        "modes": ["armed_night"],
        "effect": "allow",
        "ask_delay": 60,
    },
}


async def _setup(hass: HomeAssistant, mode: str, **changes) -> tuple[MockConfigEntry, list]:
    _states(hass, mode)
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    trigger = async_mock_service(hass, "alarm_control_panel", "alarm_trigger")
    settings = {k: (dict(v) if isinstance(v, dict) else v) for k, v in SETTINGS.items()}
    for key, value in changes.items():
        settings[key] = {**settings[key], **value}
    entry.runtime_data.alarm.set_settings(settings)
    await hass.async_block_till_done()
    return entry, trigger


async def test_rules_per_mode(hass: HomeAssistant, notify_calls) -> None:
    entry, trigger = await _setup(
        hass, "armed_night", rules={HALL: ["armed_away"], WINDOW: ["armed_away", "armed_night"]}
    )
    hass.states.async_set(HALL, "on", {"friendly_name": "Flur"})
    await hass.async_block_till_done()
    assert trigger == []  # motion does not count at night
    hass.states.async_set(WINDOW, "on", {"friendly_name": "Fenster WZ"})
    await hass.async_block_till_done()
    assert len(trigger) == 1
    assert trigger[0].data["entity_id"] == PANEL
    assert hass.states.get("sensor.security_alarm_reason").state == "Fenster WZ"

    # disarmed: nothing
    hass.states.async_set(PANEL, "disarmed", {})
    hass.states.async_set(WINDOW, "off", {})
    hass.states.async_set(WINDOW, "on", {})
    await hass.async_block_till_done()
    assert len(trigger) == 1

    # logic switched off: nothing either
    hass.states.async_set(PANEL, "armed_away", {})
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.security_alarm_logic"}, blocking=True
    )
    hass.states.async_set(WINDOW, "off", {})
    hass.states.async_set(WINDOW, "on", {})
    await hass.async_block_till_done()
    assert len(trigger) == 1


async def test_guests(hass: HomeAssistant, notify_calls, freezer) -> None:
    entry, trigger = await _setup(hass, "armed_home")
    alarm = entry.runtime_data.alarm
    assert hass.states.get("switch.security_guests").state == "off"
    hass.states.async_set("device_tracker.gast", "home", {})
    await hass.async_block_till_done()
    assert hass.states.get("switch.security_guests").state == "on"

    hass.states.async_set(HALL, "on", {})
    await hass.async_block_till_done()
    assert trigger == []  # silent with guests
    hass.states.async_set(WINDOW, "on", {})
    await hass.async_block_till_done()
    assert len(trigger) == 1  # windows still count

    # guest leaves: off after 30 minutes
    hass.states.async_set("device_tracker.gast", "not_home", {})
    await hass.async_block_till_done()
    assert alarm.guests_on
    freezer.tick(timedelta(minutes=31))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("switch.security_guests").state == "off"
    hass.states.async_set(HALL, "off", {})
    hass.states.async_set(HALL, "on", {})
    await hass.async_block_till_done()
    assert len(trigger) == 2


async def test_nightwalk_allow(hass: HomeAssistant, notify_calls, freezer) -> None:
    entry, trigger = await _setup(hass, "armed_night", nightwalk={"enabled": True})
    alarm = entry.runtime_data.alarm

    # downstairs moved 2 minutes ago: upstairs motion opens nothing
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(HALL, "on", {})
    hass.states.async_set(HALL, "off", {})
    trigger.clear()
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set(UP, "on", {})
    await hass.async_block_till_done()
    assert not alarm.nightwalk_until

    # quiet for 6 minutes: upstairs motion opens the window
    hass.states.async_set(UP, "off", {})
    freezer.tick(timedelta(minutes=6))
    hass.states.async_set(UP, "on", {})
    await hass.async_block_till_done()
    assert alarm.nightwalk_until
    assert hass.states.get("sensor.security_nightwalk_until").state not in (
        "unknown",
        "unavailable",
    )
    count = len(trigger)
    hass.states.async_set(HALL, "on", {})
    await hass.async_block_till_done()
    assert len(trigger) == count  # no burglary

    # window closed after 3 minutes: downstairs counts again
    hass.states.async_set(HALL, "off", {})
    freezer.tick(timedelta(minutes=4))
    hass.states.async_set(HALL, "on", {})
    await hass.async_block_till_done()
    assert len(trigger) == count + 1


async def test_nightwalk_ask(hass: HomeAssistant, notify_calls) -> None:
    entry, trigger = await _setup(
        hass, "armed_night", nightwalk={"enabled": True, "effect": "ask", "ask_delay": 0.3}
    )
    alarm = entry.runtime_data.alarm
    alarm.open_nightwalk()
    hass.states.async_set(HALL, "on", {"friendly_name": "Flur"})
    # not async_block_till_done: it would wait for the question to time out
    await asyncio.sleep(0.05)
    ask = [c for c in notify_calls if c.data["title"] == "🌙 Bewegung unten"]
    assert len(ask) == 1
    assert ask[0].data["data"]["actions"][0]["action"] == "SH_NACHTWEG_OK"
    hass.bus.async_fire("mobile_app_notification_action", {"action": "SH_NACHTWEG_OK"})
    await asyncio.sleep(0.05)
    await hass.async_block_till_done()
    assert trigger == []

    # no answer: alarm
    hass.states.async_set(HALL, "off", {})
    hass.states.async_set(HALL, "on", {"friendly_name": "Flur"})
    await hass.async_block_till_done()
    await asyncio.sleep(0.5)
    await hass.async_block_till_done()
    assert len(trigger) == 1
    assert "ohne Bestätigung" in hass.states.get("sensor.security_alarm_reason").state


async def test_reaction(hass: HomeAssistant, notify_calls) -> None:
    entry, trigger = await _setup(hass, "armed_away")
    created = async_mock_service(hass, "persistent_notification", "create")
    siren_on = async_mock_service(hass, "siren", "turn_on")
    siren_off = async_mock_service(hass, "siren", "turn_off")

    hass.states.async_set(PANEL, "pending", {})
    await hass.async_block_till_done()
    assert notify_calls[-1].data["title"] == "⚠️ Eingangsverzögerung"

    hass.states.async_set(WINDOW, "on", {"friendly_name": "Fenster WZ"})
    await hass.async_block_till_done()
    hass.states.async_set(PANEL, "triggered", {})
    await hass.async_block_till_done()
    assert created[-1].data["message"].startswith("Ausgelöst durch Fenster WZ")
    alarm_push = notify_calls[-1].data
    assert alarm_push["title"] == "🚨 Alarm ausgelöst"
    assert alarm_push["data"]["push"]["interruption-level"] == "critical"
    assert siren_on[-1].data["entity_id"] == ["siren.sirene"]

    hass.states.async_set(PANEL, "disarmed", {})
    await hass.async_block_till_done()
    assert siren_off[-1].data["entity_id"] == ["siren.sirene"]
    assert notify_calls[-1].data["title"] == "✅ Alarm beendet"


async def test_settings_via_websocket(hass: HomeAssistant, hass_ws_client) -> None:
    _states(hass, "disarmed")
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("switch.security_guests").state == "unavailable"
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "smarthome_ui/alarm/settings", **SETTINGS, "guests_on": True}
    )
    res = await client.receive_json()
    assert res["success"]
    assert res["result"]["configured"] is True
    assert res["result"]["guests_on"] is True
    await hass.async_block_till_done()
    assert hass.states.get("switch.security_guests").state == "on"
    assert hass.states.get("switch.security_alarm_logic").state == "on"


async def test_old_automations_switched(hass: HomeAssistant, notify_calls) -> None:
    hass.states.async_set("automation.alarm_ausloser", "on", {"id": "smarthome_alarm_regeln"})
    hass.states.async_set("automation.andere", "on", {"id": "something_else"})
    _states(hass, "disarmed")
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    off = async_mock_service(hass, "automation", "turn_off")
    on = async_mock_service(hass, "automation", "turn_on")
    entry.runtime_data.alarm.set_settings(dict(SETTINGS))
    await hass.async_block_till_done()
    assert off[-1].data["entity_id"] == ["automation.alarm_ausloser"]
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.security_alarm_logic"}, blocking=True
    )
    await hass.async_block_till_done()
    assert on[-1].data["entity_id"] == ["automation.alarm_ausloser"]


async def test_option_entities(hass: HomeAssistant, notify_calls) -> None:
    entry, _trigger = await _setup(hass, "armed_night")
    alarm = entry.runtime_data.alarm
    assert hass.states.get("switch.security_nightwalk").state == "off"
    assert hass.states.get("number.security_nightwalk_window").state == "3.0"
    assert hass.states.get("select.security_nightwalk_effect").state == "allow"
    assert hass.states.get("number.security_guests_away").state == "30.0"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.security_nightwalk"}, blocking=True
    )
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.security_nightwalk_window", "value": 7},
        blocking=True,
    )
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.security_nightwalk_effect", "option": "ask"},
        blocking=True,
    )
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.security_alarm_critical"}, blocking=True
    )
    walk = alarm.nightwalk()
    assert walk["enabled"] is True
    assert walk["window"] == 7
    assert walk["effect"] == "ask"
    assert walk["starts"] == [UP]  # lists are kept
    assert alarm.actions()["critical"] is False
    assert alarm.info()["settings"]["nightwalk"]["window"] == 7
    await hass.async_block_till_done()
    assert hass.states.get("switch.security_nightwalk").state == "on"


async def test_options_unavailable_before_handover(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("number.security_nightwalk_window").state == "unavailable"
    assert hass.states.get("select.security_nightwalk_effect").state == "unavailable"
