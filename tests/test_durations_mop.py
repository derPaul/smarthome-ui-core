"""Usual duration per area / room with warnings; vacuum first, then mop."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import async_fire_time_changed, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er

from .test_mower import _mower_device, _setup as _setup_mower
from .test_planner import _setup as _setup_planner
from .test_vacuum import _vacuum_device


async def test_mower_unusual_duration(hass: HomeAssistant, notify_calls) -> None:
    device_id = _mower_device(hass)
    entry = await _setup_mower(hass, device_id, {"gd.mower": {"push": True}})
    tracker = next(iter(entry.runtime_data.mowers.trackers.values()))
    for minutes in (40, 42, 38):
        tracker._duration("Vorgarten", minutes)
    assert notify_calls == []
    tracker._duration("Vorgarten", 12)
    assert notify_calls[-1].data["message"] == (
        "Luba: Vorgarten nur 12 min statt üblich 40 min – nicht fertig geworden?"
    )
    tracker.evaluate()
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.garden_vorgarten_last_mowed").attributes
    assert attrs["usual_minutes"] == 39


async def test_vacuum_unusual_duration(hass: HomeAssistant, notify_calls) -> None:
    device_id = _vacuum_device(hass)
    entry = await _setup_planner(hass, "vacuum", device_id)
    entry.runtime_data.store.set_notify({"sources": {"hh.robot": {"push": True}}})
    tracker = next(iter(entry.runtime_data.vacuums.trackers.values()))
    kitchen = ar.async_get(hass).async_get_area_by_name("Küche").id

    def run(minutes: int) -> None:
        tracker._finish(
            {
                "from": 0,
                "end": 1_000_000,
                "dwell": {kitchen: {"ms": minutes * 60_000, "last": 1_000_000}},
                "clean_ms": minutes * 60_000,
                "max_area": 0,
                "mop": False,
            }
        )

    for minutes in (10, 11, 9):
        run(minutes)
    run(30)
    assert notify_calls[-1].data["message"] == (
        "Robbi: Küche 30 min statt üblich 10 min – viel Schmutz oder Hindernisse?"
    )
    await hass.async_block_till_done()
    attrs = hass.states.get("sensor.household_kuche_last_vacuumed").attributes
    assert attrs["usual_minutes"] == 10
    assert attrs["last_minutes"] == 30


async def test_vacuum_first_then_mop(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 12:00:00")
    device_id = _vacuum_device(hass)
    other = next(e for e in hass.config_entries.async_entries("roborock"))
    er.async_get(hass).async_get_or_create(
        "select",
        "roborock",
        "mop",
        device_id=device_id,
        config_entry=other,
        suggested_object_id="robbi_mop_intensity",
        translation_key="mop_intensity",
    )
    hass.states.async_set(
        "select.robbi_mop_intensity", "medium", {"options": ["off", "low", "medium", "high"]}
    )
    entry = await _setup_planner(hass, "vacuum", device_id, announce=0, cond_away=False)
    select = async_mock_service(hass, "select", "select_option")
    clean = async_mock_service(hass, "vacuum", "clean_area")
    runtime = entry.runtime_data
    tracker = next(iter(runtime.vacuums.trackers.values()))
    kitchen = ar.async_get(hass).async_get_area_by_name("Küche").id
    runtime.store.set_data({"household": {"cleaned": {kitchen: {"r": 1000, "vac": tracker.id}}}})
    runtime.store.set_tasks(
        [
            {
                "key": f"clean:{kitchen}",
                "name": "Küche putzen",
                "interval": 2,
                "last": 1000,
                "due": 2000,
            }
        ]
    )
    tracker.evaluate()
    await hass.async_block_till_done()
    assert hass.states.get("switch.household_kuche_mop").state == "on"

    planner = runtime.planners.planners[tracker.id]
    planner.set_on(True)
    await hass.async_block_till_done()
    # first pass: vacuum only
    assert planner.status == "started"
    assert select[0].data == {"entity_id": "select.robbi_mop_intensity", "option": "off"}
    assert clean[0].data["cleaning_area_id"] == [kitchen]
    assert hass.states.get("sensor.household_robbi_planner").attributes["mop_due"] == ["Küche"]

    # the robot vacuums (mop off) and comes back
    hass.states.async_set(
        "select.robbi_mop_intensity", "off", {"options": ["off", "low", "medium", "high"]}
    )
    hass.states.async_set("sensor.robbi_current_room", "Küche", {})
    hass.states.async_set("vacuum.robbi", "cleaning", {})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=8))
    hass.states.async_set("vacuum.robbi", "docked", {})
    await hass.async_block_till_done()
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert planner.status == "mop_pending"
    cleaned = runtime.store.data["data"]["household"]["cleaned"][kitchen]
    assert "w" not in cleaned

    # second pass after the robot rested: mop
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert planner.status == "mopping"
    assert select[-1].data["option"] == "medium"
    assert len(clean) == 2

    # mopping run → mopped
    hass.states.async_set(
        "select.robbi_mop_intensity", "medium", {"options": ["off", "low", "medium", "high"]}
    )
    hass.states.async_set("vacuum.robbi", "cleaning", {})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=10))
    hass.states.async_set("vacuum.robbi", "docked", {})
    await hass.async_block_till_done()
    assert runtime.store.data["data"]["household"]["cleaned"][kitchen]["w"] > 0
    assert planner.mop_due_areas() == []
