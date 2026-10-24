"""Motion light: central brightness, suggestions per room, the blueprint."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.smarthome_ui.lights import BLUEPRINT, suggest
from homeassistant.components.blueprint import models
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml


def _room(hass: HomeAssistant, name: str, motion: str, lights: list[str]) -> str:
    area = ar.async_get(hass).async_create(name)
    reg = er.async_get(hass)
    m = reg.async_get_or_create(
        "binary_sensor", "test", motion, suggested_object_id=motion, original_device_class="motion"
    )
    reg.async_update_entity(m.entity_id, area_id=area.id)
    for light in lights:
        e = reg.async_get_or_create("light", "test", light, suggested_object_id=light)
        reg.async_update_entity(e.entity_id, area_id=area.id)
    return area.id


async def test_settings_and_entities(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    assert hass.states.get("number.lights_brightness_day").state == "100.0"
    assert hass.states.get("number.lights_brightness_night").state == "20.0"
    assert hass.states.get("time.lights_night_start").state == "22:00:00"
    client = await hass_ws_client(hass)
    await client.send_json(
        {"id": 1, "type": "smarthome_ui/lists/value", "path": "weather.entity", "value": None}
    )
    await client.receive_json()
    await client.send_json(
        {"id": 2, "type": "smarthome_ui/lights/settings", "night": 15, "night_start": "21:30"}
    )
    res = (await client.receive_json())["result"]
    assert res["night"] == 15 and res["night_start"] == "21:30"
    await hass.async_block_till_done()
    assert hass.states.get("number.lights_brightness_night").state == "15.0"
    assert hass.states.get("time.lights_night_start").state == "21:30:00"
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.lights_brightness_day", "value": 80},
        blocking=True,
    )
    assert entry.runtime_data.lights.settings()["day"] == 80


async def test_suggest_rooms(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client) -> None:
    _room(hass, "Flur", "flur_bewegung", ["flur_decke"])
    _room(hass, "Bad", "bad_bewegung", ["bad_spiegel", "bad_decke"])
    _room(hass, "Keller", "keller_bewegung", [])  # no light: no suggestion
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "bad_licht",
                "alias": "Bad Licht",
                "triggers": [{"trigger": "state", "entity_id": "binary_sensor.bad_bewegung"}],
                "actions": [
                    {"action": "light.turn_on", "target": {"entity_id": "light.bad_decke"}}
                ],
            }
        },
    )
    await hass.async_block_till_done()
    rooms = {r["area"]: r for r in suggest(hass)}
    assert set(rooms) == {"Flur", "Bad"}
    assert rooms["Flur"]["automation"] is None
    assert rooms["Flur"]["motion"] == ["binary_sensor.flur_bewegung"]
    assert rooms["Bad"]["automation"]["name"] == "Bad Licht"
    assert rooms["Bad"]["automation"]["ours"] is False
    assert rooms["Bad"]["lights"] == ["light.bad_decke", "light.bad_spiegel"]
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_ui/lights/suggest"})
    res = (await client.receive_json())["result"]
    assert res["rooms"][0]["area"] == "Flur"  # open suggestions first
    assert res["settings"]["blueprint"] == BLUEPRINT


def test_blueprint_valid() -> None:
    path = (
        Path(__file__).parent.parent / "custom_components/smarthome_ui/blueprints/motion_light.yaml"
    )
    bp = models.Blueprint(load_yaml(path), expected_domain="automation", schema=BLUEPRINT_SCHEMA)
    assert set(bp.inputs) >= {"motion", "light", "delay", "day_brightness", "night_brightness"}


async def test_blueprint_runs(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    """Light on with motion (day / night brightness from Core), off after the delay."""
    freezer.move_to(
        dt_util.as_utc(datetime(2026, 10, 24, 12, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    on = async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    hass.states.async_set("binary_sensor.flur_bewegung", "off")
    hass.states.async_set("light.flur", "off")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "flur",
                "alias": "Flur Bewegungslicht",
                "use_blueprint": {
                    "path": BLUEPRINT,
                    "input": {
                        "motion": ["binary_sensor.flur_bewegung"],
                        "light": ["light.flur"],
                        "delay": 60,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.flur_bewegung", "on")
    await hass.async_block_till_done()
    assert on[-1].data["brightness_pct"] == 100
    assert on[-1].data["entity_id"] == ["light.flur"]
    hass.states.async_set("binary_sensor.flur_bewegung", "off")
    await hass.async_block_till_done()
    assert not off
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert off[-1].data["entity_id"] == ["light.flur"]
    # at night the night brightness
    freezer.move_to(
        dt_util.as_utc(datetime(2026, 10, 24, 23, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    hass.states.async_set("binary_sensor.flur_bewegung", "on")
    await hass.async_block_till_done()
    assert on[-1].data["brightness_pct"] == 20
