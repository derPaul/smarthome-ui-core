"""Action "vacuum rooms" and the blueprint "vacuum when everybody left"."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import area_registry as ar
from homeassistant.setup import async_setup_component

from .test_planner import _setup
from .test_vacuum import _vacuum_device


async def test_vacuum_rooms_action(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 12:00:00")
    device_id = _vacuum_device(hass)
    entry = await _setup(hass, "vacuum", device_id)
    clean = async_mock_service(hass, "vacuum", "clean_area")
    tracker = next(iter(entry.runtime_data.vacuums.trackers.values()))
    hall = ar.async_get(hass).async_get_area_by_name("Flur").id
    kitchen = ar.async_get(hass).async_get_area_by_name("Küche").id
    # kitchen vacuumed one hour ago
    entry.runtime_data.store.set_data(
        {"household": {"cleaned": {kitchen: {"r": 1772449200000 - 3600_000, "vac": tracker.id}}}}
    )

    result = await hass.services.async_call(
        "smarthome_ui",
        "vacuum_rooms",
        {"entity_id": "vacuum.robbi", "areas": [hall, kitchen], "skip_hours": 4},
        blocking=True,
        return_response=True,
    )
    await hass.async_block_till_done()
    assert result == {"started": [hall]}
    assert clean[0].data == {"entity_id": "vacuum.robbi", "cleaning_area_id": [hall]}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "smarthome_ui",
            "vacuum_rooms",
            {"entity_id": "vacuum.other", "areas": [hall]},
            blocking=True,
        )


async def test_blueprint_when_everybody_left(hass: HomeAssistant, notify_calls) -> None:
    device_id = _vacuum_device(hass)
    await _setup(hass, "vacuum", device_id)
    path = Path(hass.config.path("blueprints/automation/smarthome_ui/vacuum_when_leaving.yaml"))
    assert path.exists()
    hall = ar.async_get(hass).async_get_area_by_name("Flur").id
    calls = async_mock_service(hass, "smarthome_ui", "vacuum_rooms")
    hass.states.async_set("zone.home", "2")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "use_blueprint": {
                    "path": "smarthome_ui/vacuum_when_leaving.yaml",
                    "input": {
                        "vacuum": "vacuum.robbi",
                        "areas": [hall],
                        "delay": 0,
                        "skip_hours": 4,
                        "after": "00:00:00",
                        "before": "23:59:59",
                    },
                }
            }
        },
    )
    await hass.async_block_till_done()
    hass.states.async_set("zone.home", "1")
    await hass.async_block_till_done()
    assert calls == []
    hass.states.async_set("zone.home", "0")  # the last one left
    await hass.async_block_till_done()
    assert len(calls) == 1
    assert calls[0].data == {
        "entity_id": "vacuum.robbi",
        "areas": [hall],
        "skip_hours": 4,
        "vacuum_only": True,
    }
