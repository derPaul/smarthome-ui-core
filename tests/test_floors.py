"""Robot vacuum: only rooms on its floor; tell when rooms are due on another floor."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
    floor_registry as fr,
)

from .test_planner import _setup
from .test_vacuum import _vacuum_device


async def test_vacuum_floors(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 12:00:00")
    floors = fr.async_get(hass)
    ground = floors.async_create("Erdgeschoss", level=0)
    upper = floors.async_create("Obergeschoss", level=1)
    device_id = _vacuum_device(hass)
    areas = ar.async_get(hass)
    kitchen = areas.async_get_area_by_name("Küche")
    bath = areas.async_get_area_by_name("Bad")
    areas.async_update(kitchen.id, floor_id=ground.floor_id)
    areas.async_update(bath.id, floor_id=upper.floor_id)
    dr.async_get(hass).async_update_device(device_id, area_id=kitchen.id)  # robot downstairs

    entry = await _setup(hass, "vacuum", device_id, announce=0, cond_away=False)
    clean = async_mock_service(hass, "vacuum", "clean_area")
    runtime = entry.runtime_data
    runtime.store.set_notify({"sources": {"hh.robot": {"push": True}}})
    tracker = next(iter(runtime.vacuums.trackers.values()))
    assert tracker.floor_id() == ground.floor_id
    assert tracker.reachable(kitchen.id)
    assert not tracker.reachable(bath.id)

    cleaned = {a.id: {"r": 1000, "vac": tracker.id} for a in (kitchen, bath)}
    runtime.store.set_data({"household": {"cleaned": cleaned}})
    runtime.store.set_tasks(
        [
            {
                "key": f"clean:{a.id}",
                "name": f"{a.name} putzen",
                "interval": 2,
                "last": 1000,
                "due": 2000,
            }
            for a in (kitchen, bath)
        ]
    )
    tracker.evaluate()
    planner = runtime.planners.planners[tracker.id]
    planner.set_on(True)
    await hass.async_block_till_done()

    assert planner.status == "started"
    assert clean[0].data["cleaning_area_id"] == [kitchen.id]
    assert notify_calls[-1].data["message"] == (
        "Robbi steht im Erdgeschoss – fällig sind Obergeschoss: Bad. Roboter umsetzen?"
    )
    attrs = hass.states.get("sensor.household_robbi_planner").attributes
    assert attrs["unreachable"] == ["Bad"]

    # carried upstairs and the map switched (Roborock: one map per floor)
    other = next(iter(hass.config_entries.async_entries("roborock")))
    er.async_get(hass).async_get_or_create(
        "select",
        "roborock",
        "map",
        device_id=device_id,
        config_entry=other,
        suggested_object_id="robbi_selected_map",
    )
    hass.states.async_set(
        "select.robbi_selected_map", "Obergeschoss", {"options": ["Erdgeschoss", "Obergeschoss"]}
    )
    assert tracker.floor_id() == upper.floor_id
    assert tracker.reachable(bath.id)
    assert not tracker.reachable(kitchen.id)
