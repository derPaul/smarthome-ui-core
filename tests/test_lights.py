"""Motion light: central brightness, suggestions per room, the blueprint."""

from __future__ import annotations

import asyncio
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
    assert hass.states.get("number.lights_delay").state == "120.0"
    await client.send_json({"id": 3, "type": "smarthome_ui/lights/settings", "delay": 45})
    assert (await client.receive_json())["result"]["delay"] == 45
    await hass.async_block_till_done()
    assert hass.states.get("number.lights_delay").state == "45.0"
    assert hass.states.get("number.lights_fade_on").state == "1.0"
    await client.send_json({"id": 4, "type": "smarthome_ui/lights/settings", "fade_off": 2.3})
    assert (await client.receive_json())["result"]["fade_off"] == 2.5


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


async def test_suggest_prefers_motion_light(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """An old automation left disabled next to the new motion light: the room shows ours."""
    _room(hass, "Flur", "flur_bewegung", ["flur_decke"])
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "alt",
                    "alias": "Flur alt",
                    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.flur_bewegung"}],
                    "actions": [
                        {"action": "light.turn_on", "target": {"entity_id": "light.flur_decke"}}
                    ],
                },
                {
                    "id": "neu",
                    "alias": "Flur Bewegungslicht",
                    "use_blueprint": {
                        "path": BLUEPRINT,
                        "input": {
                            "motion": ["binary_sensor.flur_bewegung"],
                            "light": ["light.flur_decke"],
                        },
                    },
                },
            ]
        },
    )
    await hass.async_block_till_done()
    await hass.services.async_call(
        "automation", "turn_off", {"entity_id": "automation.flur_alt"}, blocking=True
    )
    rooms = {r["area"]: r for r in suggest(hass)}
    assert rooms["Flur"]["automation"]["name"] == "Flur Bewegungslicht"
    assert rooms["Flur"]["automation"]["ours"] is True


def test_blueprint_valid() -> None:
    path = (
        Path(__file__).parent.parent / "custom_components/smarthome_ui/blueprints/motion_light.yaml"
    )
    bp = models.Blueprint(load_yaml(path), expected_domain="automation", schema=BLUEPRINT_SCHEMA)
    assert set(bp.inputs) >= {
        "motion",
        "light",
        "delay",
        "default_delay",
        "day_pct",
        "night_pct",
        "fade_on",
        "fade_off",
        "day_brightness",
        "night_brightness",
    }


async def test_blueprint_central_delay(
    hass: HomeAssistant, entry: MockConfigEntry, freezer
) -> None:
    """Without an own delay the light goes off after Core's standard delay."""
    freezer.move_to(
        dt_util.as_utc(datetime(2026, 10, 24, 12, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    entry.runtime_data.lights.set("delay", 30)
    await hass.async_block_till_done()
    async_mock_service(hass, "light", "turn_on")
    off = async_mock_service(hass, "light", "turn_off")
    hass.states.async_set("binary_sensor.bad_bewegung", "off")
    hass.states.async_set("light.bad", "off")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "bad",
                "alias": "Bad Bewegungslicht",
                "use_blueprint": {
                    "path": BLUEPRINT,
                    "input": {"motion": ["binary_sensor.bad_bewegung"], "light": ["light.bad"]},
                },
            }
        },
    )
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.bad_bewegung", "on")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.bad_bewegung", "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(seconds=25))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not off
    freezer.tick(timedelta(seconds=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert off[-1].data["entity_id"] == ["light.bad"]


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
    assert on[-1].data["transition"] == 1  # central fade
    hass.states.async_set("binary_sensor.flur_bewegung", "off")
    await hass.async_block_till_done()
    assert not off
    freezer.tick(timedelta(seconds=61))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert off[-1].data["entity_id"] == ["light.flur"]
    assert off[-1].data["transition"] == 1
    # at night the night brightness
    freezer.move_to(
        dt_util.as_utc(datetime(2026, 10, 24, 23, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    hass.states.async_set("binary_sensor.flur_bewegung", "on")
    await hass.async_block_till_done()
    assert on[-1].data["brightness_pct"] == 20


async def test_blueprint_own_brightness(
    hass: HomeAssistant, entry: MockConfigEntry, freezer
) -> None:
    """An automation's own day / night brightness wins over the central one; 0 keeps central."""
    freezer.move_to(
        dt_util.as_utc(datetime(2026, 10, 24, 12, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    on = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("binary_sensor.kind_bewegung", "off")
    hass.states.async_set("light.kind", "off")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "kind",
                "alias": "Kind Bewegungslicht",
                "use_blueprint": {
                    "path": BLUEPRINT,
                    "input": {
                        "motion": ["binary_sensor.kind_bewegung"],
                        "light": ["light.kind"],
                        "day_pct": 0,
                        "night_pct": 5,
                        "fade_on": 0,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.kind_bewegung", "on")
    await hass.async_block_till_done()
    assert on[-1].data["brightness_pct"] == 100  # day: central
    assert on[-1].data["transition"] == 0  # own fade: switch hard
    hass.states.async_set("binary_sensor.kind_bewegung", "off")
    await hass.async_block_till_done()
    freezer.move_to(
        dt_util.as_utc(datetime(2026, 10, 24, 23, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    hass.states.async_set("binary_sensor.kind_bewegung", "on")
    await hass.async_block_till_done()
    assert on[-1].data["brightness_pct"] == 5  # night: own


async def test_blueprint_waits_for_stale_lux(
    hass: HomeAssistant, entry: MockConfigEntry, freezer
) -> None:
    """Right after the light went off the sensor still reports that light: wait for a new value."""

    async def _settle() -> None:
        # the run is waiting: block_till_done would wait for it, so only let the loop run a while
        for _ in range(20):
            await asyncio.sleep(0)

    freezer.move_to(
        dt_util.as_utc(datetime(2026, 10, 24, 21, 0, tzinfo=dt_util.get_default_time_zone()))
    )
    on = async_mock_service(hass, "light", "turn_on")
    hass.states.async_set("binary_sensor.hwr_bewegung", "off")
    hass.states.async_set("light.hwr", "off")
    hass.states.async_set("sensor.hwr_lux", "13.5", {"device_class": "illuminance"})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "hwr",
                "alias": "HWR Bewegungslicht",
                "use_blueprint": {
                    "path": BLUEPRINT,
                    "input": {
                        "motion": ["binary_sensor.hwr_bewegung"],
                        "light": ["light.hwr"],
                        "illuminance": "sensor.hwr_lux",
                        "lux_max": 10,
                    },
                },
            }
        },
    )
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.hwr_bewegung", "on")
    await _settle()
    assert not on  # still the brightness with the light on
    hass.states.async_set("sensor.hwr_lux", "1.0", {"device_class": "illuminance"})
    await _settle()
    assert on[-1].data["entity_id"] == ["light.hwr"]
    # really bright: gives up after 30 s
    on.clear()
    hass.states.async_set("binary_sensor.hwr_bewegung", "off")
    hass.states.async_set("sensor.hwr_lux", "400", {"device_class": "illuminance"})
    await _settle()
    hass.states.async_set("binary_sensor.hwr_bewegung", "on")
    await _settle()
    freezer.tick(timedelta(seconds=31))
    async_fire_time_changed(hass)
    await _settle()
    assert not on


async def test_covered_by_area_device_and_group(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Automations switching the light via the area or a light group count too."""
    _room(hass, "Küche", "kuche_bewegung", ["kuche_decke"])
    _room(hass, "Flur", "flur_bewegung", ["flur_decke"])
    _room(hass, "Bad", "bad_bewegung", ["bad_decke"])
    area_kuche = next(a for a in ar.async_get(hass).areas.values() if a.name == "Küche").id
    hass.states.async_set("light.flur_gruppe", "off", {"entity_id": ["light.flur_decke"]})
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "kuche",
                    "alias": "Küche per Bereich",
                    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.kuche_bewegung"}],
                    "actions": [{"action": "light.turn_on", "target": {"area_id": area_kuche}}],
                },
                {
                    "id": "flur",
                    "alias": "Flur per Gruppe",
                    "triggers": [{"trigger": "state", "entity_id": "binary_sensor.flur_bewegung"}],
                    "actions": [
                        {"action": "light.turn_on", "target": {"entity_id": "light.flur_gruppe"}}
                    ],
                },
            ]
        },
    )
    await hass.async_block_till_done()
    rooms = {r["area"]: r for r in suggest(hass, [])}
    assert rooms["Küche"]["automation"]["name"] == "Küche per Bereich"
    assert rooms["Flur"]["automation"]["name"] == "Flur per Gruppe"
    assert rooms["Bad"]["automation"] is None


async def test_ignore_room(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client) -> None:
    area = _room(hass, "Bad", "bad_bewegung", ["bad_decke"])
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_ui/lights/ignore", "area_id": area})
    res = (await client.receive_json())["result"]
    assert res["rooms"][0]["ignored"] is True
    await client.send_json(
        {"id": 2, "type": "smarthome_ui/lights/ignore", "area_id": area, "ignore": False}
    )
    assert (await client.receive_json())["result"]["rooms"][0]["ignored"] is False


async def test_label_and_category(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Automations from Core's blueprints get the label Smart-Core and the category Licht."""
    from homeassistant.helpers import category_registry as cr, label_registry as lr

    _room(hass, "Flur", "flur_bewegung", ["flur"])
    hass.states.async_set("binary_sensor.flur_bewegung", "off")
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": [
                {
                    "id": "flur",
                    "alias": "Bewegungslicht Flur",
                    "use_blueprint": {
                        "path": BLUEPRINT,
                        "input": {
                            "motion": ["binary_sensor.flur_bewegung"],
                            "light": ["light.flur"],
                        },
                    },
                },
                {"id": "andere", "alias": "Andere", "triggers": [], "actions": []},
            ]
        },
    )
    await hass.async_block_till_done()
    hass.bus.async_fire("automation_reloaded")
    await hass.async_block_till_done()
    reg = er.async_get(hass)
    label = lr.async_get(hass).async_get_label_by_name("Smart-Core")
    flur = reg.async_get("automation.bewegungslicht_flur")
    assert label.label_id in flur.labels
    cat = cr.async_get(hass).async_get_category(
        scope="automation", category_id=flur.categories["automation"]
    )
    assert cat.name == "Licht"
    assert not reg.async_get("automation.andere").labels
    # the room counts as covered by its own motion light, with the blueprint's inputs
    flur_room = next(r for r in suggest(hass, []) if r["area"] == "Flur")
    assert flur_room["automation"]["ours"] is True
    assert flur_room["automation"]["input"]["light"] == ["light.flur"]
