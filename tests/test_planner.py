"""Robot planner: switches, conditions (solar, at home, window), announcement, start, skip."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.smarthome_ui.const import DOMAIN
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .test_mower import _mower_device
from .test_vacuum import _vacuum_device

ALL_DAY = {"auto_from": "00:00", "auto_to": "23:59"}


async def _setup(hass: HomeAssistant, kind: str, device_id: str, **settings) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Smarthome UI Core",
        subentries_data=[
            ConfigSubentryData(
                data={"device_id": device_id, **ALL_DAY, **settings},
                subentry_type=kind,
                title="Luba" if kind == "mower" else "Robbi",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_mower_planner(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-06-10 12:00:00")
    hass.states.async_set("sensor.pv_power", "400", {"unit_of_measurement": "W"})
    device_id = _mower_device(hass)
    entry = await _setup(
        hass, "mower", device_id, cond_solar=True, solar_entity="sensor.pv_power", solar_min=1000
    )
    # after the set-up (the switch component registers its own services)
    start = async_mock_service(hass, "lawn_mower", "start_mowing")
    on = async_mock_service(hass, "switch", "turn_on")
    off = async_mock_service(hass, "switch", "turn_off")
    runtime = entry.runtime_data
    runtime.store.set_notify({"sources": {"gd.mower": {"push": True}}})
    tracker = next(iter(runtime.mowers.trackers.values()))
    # Vorgarten mowed 3 days ago → due (June: every 2 days); "Hinten" never mowed → not due
    runtime.store.set_mow_log({"Vorgarten": {"last": 1780833600000, "mode": "Kanten", "mins": 30}})
    tracker.evaluate()

    assert hass.states.get("sensor.garden_luba_planner").state == "off"
    assert hass.states.get("switch.garden_luba_auto").state == "off"
    assert hass.states.get("switch.garden_vorgarten_auto").state == "on"

    # (switch.turn_on is mocked here – switch the planner on directly)
    planner = runtime.planners.planners[tracker.id]
    planner.set_on(True)
    await hass.async_block_till_done()
    state = hass.states.get("sensor.garden_luba_planner")
    assert state.state == "waiting_solar"
    assert state.attributes["due"] == ["Vorgarten"]

    # enough solar → announced (push with "Heute nicht") → start after 10 minutes
    hass.states.async_set("sensor.pv_power", "1.5", {"unit_of_measurement": "kW"})
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_luba_planner").state == "announced"
    push = next(c.data for c in notify_calls if c.data["message"].startswith("Luba mäht ab"))
    assert push["message"].endswith(": Vorgarten")
    assert push["data"]["actions"][0]["title"] == "Heute nicht"

    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_luba_planner").state == "started"
    assert [c.data["entity_id"] for c in on] == ["switch.luba_vorgarten"]
    assert [c.data["entity_id"] for c in off] == ["switch.luba_hinten"]
    assert [c.data["entity_id"] for c in start] == ["lawn_mower.luba"]

    # only once a day
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_luba_planner").state == "done_today"
    assert len(start) == 1


async def test_skip_and_area_switch(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-06-10 12:00:00")
    device_id = _mower_device(hass)
    entry = await _setup(hass, "mower", device_id)
    runtime = entry.runtime_data
    tracker = next(iter(runtime.mowers.trackers.values()))
    runtime.store.set_mow_log({"Vorgarten": {"last": 1780833600000, "mode": "Kanten", "mins": 30}})
    tracker.evaluate()
    planner = runtime.planners.planners[tracker.id]
    planner.set_on(True)
    await hass.async_block_till_done()
    assert planner.status == "announced"

    # area switched off → nothing due
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.garden_vorgarten_auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert planner.status == "nothing_due"
    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.garden_vorgarten_auto"}, blocking=True
    )
    await hass.async_block_till_done()
    assert planner.status == "announced"

    # "Heute nicht" in the push
    hass.bus.async_fire(
        "mobile_app_notification_action", {"action": f"SMARTHOME_SKIP_{tracker.id}"}
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_luba_planner").state == "skipped"

    # next day it plans again
    freezer.tick(timedelta(days=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert planner.status == "announced"


async def test_vacuum_planner_waits_until_nobody_home(
    hass: HomeAssistant, notify_calls, freezer
) -> None:
    freezer.move_to("2026-03-02 12:00:00")
    clean = async_mock_service(hass, "vacuum", "clean_area")
    hass.states.async_set("person.paul", "home")
    device_id = _vacuum_device(hass)
    entry = await _setup(hass, "vacuum", device_id, announce=0)
    runtime = entry.runtime_data
    tracker = next(iter(runtime.vacuums.trackers.values()))
    kitchen = next(
        a for a in tracker.hass.data["area_registry"].areas.values() if a.name == "Küche"
    )
    # the room the robot knows, cleaned long ago; the dashboard's task says it is due
    runtime.store.set_data({"household": {"cleaned": {kitchen.id: {"r": 1000, "vac": tracker.id}}}})
    runtime.store.set_tasks(
        [
            {
                "key": f"clean:{kitchen.id}",
                "name": "Küche putzen",
                "interval": 2,
                "last": 1000,
                "due": 2000,
            }
        ]
    )
    tracker.evaluate()
    planner = runtime.planners.planners[tracker.id]
    planner.set_on(True)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_robbi_planner").state == "waiting_home"

    hass.states.async_set("person.paul", "not_home")
    freezer.tick(timedelta(minutes=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert planner.status == "started"
    assert clean[0].data == {"entity_id": "vacuum.robbi", "cleaning_area_id": [kitchen.id]}
    runs = runtime.store.data["data"]["household"]["runs"]
    assert runs[-1]["areas"] == [kitchen.id]


async def test_reconfigure_planner(hass: HomeAssistant, notify_calls) -> None:
    device_id = _vacuum_device(hass)
    entry = await _setup(hass, "vacuum", device_id)
    subentry_id = next(iter(entry.subentries))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            "device_id": device_id,
            "auto_from": "10:00:00",
            "auto_to": "16:00:00",
            "cond_quiet": True,
            "cond_away": False,
            "cond_solar": True,
            "solar_entity": "sensor.pv_power",
            "solar_min": 800,
            "announce": 5,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    data = entry.subentries[subentry_id].data
    assert data["auto_from"] == "10:00"
    assert data["solar_min"] == 800
    planner = entry.runtime_data.planners.planners[subentry_id]
    assert planner.settings["cond_solar"] is True
    assert planner.settings["cond_away"] is False
