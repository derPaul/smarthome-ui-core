"""Moved from the dashboard: appliance supplies, vacuum tanks, open windows, mailbox,
presence simulation, grocery months."""

from __future__ import annotations

from datetime import datetime, timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .test_appliance import _device, _setup as _setup_appliance
from .test_vacuum import _setup as _setup_vacuum, _vacuum_device


async def _core(hass: HomeAssistant, sources: dict) -> MockConfigEntry:
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.store.set_notify({"sources": sources})
    await hass.async_block_till_done()
    return entry


async def test_dishwasher_supplies(hass: HomeAssistant, notify_calls) -> None:
    device_id = _device(
        hass,
        "Geschirrspüler",
        [
            ("sensor", "spueler_operation_state", "ready", {"friendly_name": "Betriebszustand"}),
            ("sensor", "spueler_rinse_aid", "off", {"friendly_name": "Klarspüler"}),
        ],
    )
    entry = await _setup_appliance(hass, device_id, "Spüli")
    hass.states.async_set("sensor.spueler_rinse_aid", "present", {"friendly_name": "Klarspüler"})
    await hass.async_block_till_done()
    entry.runtime_data.appliances._tick()
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Spüli: Klarspüler nachfüllen"
    tracker = next(iter(entry.runtime_data.appliances.trackers.values()))
    assert tracker.status["supplies"] == ["rinse_aid"]


async def test_fridge_water_filter(hass: HomeAssistant, notify_calls) -> None:
    device_id = _device(
        hass,
        "Kühlschrank",
        [
            ("sensor", "kuehl_temp", "5", {"device_class": "temperature"}),
            ("sensor", "kuehl_water_filter", "50", {"unit_of_measurement": "%"}),
        ],
    )
    entry = await _setup_appliance(hass, device_id, "Kühli")
    hass.states.async_set("sensor.kuehl_water_filter", "8", {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    entry.runtime_data.appliances._tick()
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Kühli: Wasserfilter wechseln"


async def test_vacuum_tank_sensor(hass: HomeAssistant, notify_calls, freezer) -> None:
    device_id = _vacuum_device(hass)
    from homeassistant.helpers import entity_registry as er

    other = next(e for e in hass.config_entries.async_entries("roborock"))
    er.async_get(hass).async_get_or_create(
        "sensor",
        "roborock",
        "tank",
        device_id=device_id,
        suggested_object_id="robbi_clean_water_tank",
        config_entry=other,
    )
    hass.states.async_set("sensor.robbi_clean_water_tank", "ok", {"friendly_name": "Wassertank"})
    entry = await _setup_vacuum(hass, device_id, {"hh.robot": {"push": True}})
    rules = entry.runtime_data.vacuums.rules()
    assert any(r["id"].endswith("tank|sensor.robbi_clean_water_tank") for r in rules)


async def test_open_long(hass: HomeAssistant, notify_calls, freezer) -> None:
    hass.states.async_set(
        "binary_sensor.fenster_bad", "off", {"device_class": "window", "friendly_name": "Bad"}
    )
    entry = await _core(hass, {"sec.openings": {"push": True}})
    hass.states.async_set(
        "binary_sensor.fenster_bad", "on", {"device_class": "window", "friendly_name": "Bad"}
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Bad ist seit 10 Minuten offen"
    # closed and opened again, minutes changed by the entity
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.security_open_minutes", "value": 20},
        blocking=True,
    )
    assert entry.runtime_data.security.checks()["open_minutes"] == 20
    count = len(notify_calls)
    hass.states.async_set("binary_sensor.fenster_bad", "off", {"device_class": "window"})
    hass.states.async_set(
        "binary_sensor.fenster_bad", "on", {"device_class": "window", "friendly_name": "Bad"}
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert len(notify_calls) == count


async def test_mailbox(hass: HomeAssistant, notify_calls, freezer) -> None:
    flap, door = "binary_sensor.briefkasten_einwurf", "binary_sensor.briefkasten_entnahme"
    hass.states.async_set(flap, "off", {"friendly_name": "Briefkasten Einwurfklappe"})
    hass.states.async_set(door, "off", {"friendly_name": "Briefkasten Entnahmetür"})
    entry = await _core(hass, {"hh.mail": {"push": True}})
    mailbox = entry.runtime_data.mailbox
    assert (mailbox.flap, mailbox.door) == (flap, door)
    assert hass.states.get("binary_sensor.household_mail").state == "off"

    hass.states.async_set(flap, "on", {})
    hass.states.async_set(flap, "off", {})
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.household_mail").state == "on"
    assert notify_calls[-1].data["message"] == "Post ist da"
    # flap swings again: still one delivery, no second push
    freezer.tick(timedelta(seconds=20))
    hass.states.async_set(flap, "on", {})
    hass.states.async_set(flap, "off", {})
    await hass.async_block_till_done()
    assert len(mailbox.waiting()) == 1
    assert len(notify_calls) == 1

    # door shakes right after a delivery: not emptied
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(door, "on", {})
    freezer.tick(timedelta(seconds=3))
    hass.states.async_set(door, "off", {})
    await hass.async_block_till_done()
    assert mailbox.waiting()

    # door really opened later: emptied
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(door, "on", {})
    freezer.tick(timedelta(seconds=20))
    hass.states.async_set(door, "off", {})
    await hass.async_block_till_done()
    assert not mailbox.waiting()
    assert hass.states.get("binary_sensor.household_mail").state == "off"

    # new post, emptied by the button
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set(flap, "on", {})
    hass.states.async_set(flap, "off", {})
    await hass.async_block_till_done()
    assert len(notify_calls) == 2
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.household_mail_emptied"}, blocking=True
    )
    await hass.async_block_till_done()
    assert not mailbox.waiting()
    assert mailbox.info()["empty_by"] == "hand"

    # flap left open
    hass.states.async_set(flap, "on", {})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=4))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Briefkasten: Klappe steht offen"


async def test_presence_sim(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    client = await hass_ws_client(hass)
    freezer.move_to(datetime(2026, 10, 26, 20, 0, tzinfo=dt_util.get_default_time_zone()))
    hass.states.async_set("person.anna", "not_home", {})
    hass.states.async_set("light.wohnzimmer", "off", {})
    hass.states.async_set("light.kuche", "off", {})
    hass.states.async_set("automation.anwesenheit", "on", {"id": "smarthome_anwesenheit"})
    entry = await _core(hass, {})
    sim = entry.runtime_data.presence_sim
    assert hass.states.get("switch.security_presence_sim").state == "unavailable"
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    auto_off = async_mock_service(hass, "automation", "turn_off")

    await client.send_json(
        {
            "id": 1,
            "type": "smarthome_ui/presence_sim/settings",
            "enabled": True,
            "lights": ["light.wohnzimmer", "light.kuche"],
            "start": "time",
            "from": "18:00",
            "until": "23:00",
            "min": 10,
            "max": 10,
            "parallel": 1,
        }
    )
    res = await client.receive_json()
    assert res["success"] and res["result"]["state"] == "running"
    await hass.async_block_till_done()
    assert auto_off[-1].data["entity_id"] == ["automation.anwesenheit"]
    assert hass.states.get("switch.security_presence_sim").state == "on"

    import random

    random.seed(1)
    for _ in range(20):
        sim._tick()
        if on:
            break
    await hass.async_block_till_done()
    assert len(on) == 1
    light = on[0].data["entity_id"][0]
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert off[-1].data["entity_id"] == [light]

    # somebody home: nothing
    hass.states.async_set("person.anna", "home", {})
    assert sim.state() == "waiting"
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.security_presence_sim"}, blocking=True
    )
    assert sim.state() == "off"


async def test_grocery_months(hass: HomeAssistant, hass_ws_client, freezer) -> None:
    client = await hass_ws_client(hass)
    freezer.move_to("2026-11-15 12:00:00")
    hass.states.async_set("sensor.einkauf_letzter_monat", "312.40", {})
    entry = await _core(hass, {})
    await client.send_json(
        {
            "id": 1,
            "type": "smarthome_ui/grocery/settings",
            "entity": "sensor.einkauf_letzter_monat",
            "months": {"2026-09": 280.5},
        }
    )
    res = await client.receive_json()
    assert res["result"]["months"] == {"2026-09": 280.5, "2026-10": 312.4}
    hass.states.async_set("sensor.einkauf_letzter_monat", "318.00", {})
    await hass.async_block_till_done()
    assert entry.runtime_data.grocery.info()["months"]["2026-10"] == 318.0
