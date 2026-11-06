"""Heating blueprints: thermostat displays (time, presence) and PV surplus into the floor."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.components.blueprint import models
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml

FOLDER = Path(__file__).parent.parent / "custom_components/smarthome_ui/blueprints"


def _at(hour: int, minute: int = 0) -> datetime:
    return dt_util.as_utc(
        datetime(2026, 11, 6, hour, minute, tzinfo=dt_util.get_default_time_zone())
    )


@pytest.mark.parametrize(
    "name", ["thermostat_display_time", "thermostat_display_presence", "pv_floor_heating"]
)
def test_blueprints_valid(name: str) -> None:
    bp = models.Blueprint(
        load_yaml(FOLDER / f"{name}.yaml"), expected_domain="automation", schema=BLUEPRINT_SCHEMA
    )
    assert bp.metadata["source_url"].endswith(f"{name}.yaml")


def _thermostat(hass: HomeAssistant, name: str) -> str:
    """A fake Meross LAN thermostat device with its two display brightness numbers."""
    other = MockConfigEntry(domain="meross_lan")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("meross_lan", name)}, name=name
    )
    ents = er.async_get(hass)
    for kind in ("standby", "operation"):
        oid = f"{name}_screenbrightness_{kind}"
        ents.async_get_or_create(
            "number",
            "meross_lan",
            oid,
            device_id=device.id,
            suggested_object_id=oid,
            config_entry=other,
        )
        hass.states.async_set(f"number.{oid}", "50")
    return device.id


async def _automation(hass: HomeAssistant, path: str, inputs: dict) -> None:
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "test",
                "alias": "Test",
                "use_blueprint": {"path": f"smarthome_ui/{path}.yaml", "input": inputs},
            }
        },
    )
    await hass.async_block_till_done()


def _values(calls) -> dict[str, float]:
    out = {}
    for call in calls:
        for eid in call.data["entity_id"]:
            out[eid] = call.data["value"]
    return out


async def test_display_time(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    freezer.move_to(_at(21, 59))
    dev = _thermostat(hass, "buero")
    calls = async_mock_service(hass, "number", "set_value")
    await _automation(hass, "thermostat_display_time", {"thermostats": [dev]})
    freezer.move_to(_at(22, 0))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _values(calls) == {
        "number.buero_screenbrightness_standby": 0,
        "number.buero_screenbrightness_operation": 25,
    }
    calls.clear()
    freezer.move_to(_at(6, 30) + timedelta(days=1))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _values(calls) == {
        "number.buero_screenbrightness_standby": 50,
        "number.buero_screenbrightness_operation": 100,
    }


async def test_display_presence(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    freezer.move_to(_at(12))
    dev = _thermostat(hass, "kind")
    hass.states.async_set("binary_sensor.kind_praesenz", "off")
    hass.states.async_set("person.anna", "not_home")
    calls = async_mock_service(hass, "number", "set_value")
    await _automation(
        hass,
        "thermostat_display_presence",
        {
            "thermostats": [dev],
            "presence": ["binary_sensor.kind_praesenz", "person.anna"],
            "delay": 5,
        },
    )
    # someone comes in → bright at once
    hass.states.async_set("binary_sensor.kind_praesenz", "on")
    await hass.async_block_till_done()
    assert _values(calls)["number.kind_screenbrightness_standby"] == 50
    calls.clear()
    # a person at home keeps it bright when the room sensor goes off
    hass.states.async_set("person.anna", "home")
    hass.states.async_set("binary_sensor.kind_praesenz", "off")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert all(v == 50 or v == 100 for v in _values(calls).values())
    calls.clear()
    # she leaves for work (another zone): dark after the delay
    hass.states.async_set("person.anna", "Arbeit")
    await hass.async_block_till_done()
    assert not calls
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert _values(calls) == {
        "number.kind_screenbrightness_standby": 0,
        "number.kind_screenbrightness_operation": 50,
    }


async def test_display_quiet_hours(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    freezer.move_to(_at(23))
    dev = _thermostat(hass, "schlaf")
    hass.states.async_set("binary_sensor.schlaf_praesenz", "off")
    calls = async_mock_service(hass, "number", "set_value")
    await _automation(
        hass,
        "thermostat_display_presence",
        {
            "thermostats": [dev],
            "presence": ["binary_sensor.schlaf_praesenz"],
            "quiet_start": "22:00:00",
            "quiet_end": "07:00:00",
        },
    )
    hass.states.async_set("binary_sensor.schlaf_praesenz", "on")
    await hass.async_block_till_done()
    assert _values(calls)["number.schlaf_screenbrightness_standby"] == 0


async def test_pv_floor_heating(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    freezer.move_to(_at(11))
    assert await async_setup_component(hass, "scene", {})
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": {"estrich": {"name": "Estrich wird geladen"}}}
    )
    temps = async_mock_service(hass, "climate", "set_temperature")
    async_mock_service(hass, "climate", "set_hvac_mode")
    numbers = async_mock_service(hass, "number", "set_value")
    hass.states.async_set(
        "sensor.netz", "300", {"device_class": "power", "unit_of_measurement": "W"}
    )
    hass.states.async_set("sensor.akku", "95", {"device_class": "battery"})
    hass.states.async_set("climate.bad", "heat", {"temperature": 22, "max_temp": 30})
    hass.states.async_set("climate.buero", "heat", {"temperature": 21, "max_temp": 30})
    hass.states.async_set("number.hk_tag", "22", {"min": 5, "max": 30})
    await hass.async_block_till_done()
    await _automation(
        hass,
        "pv_floor_heating",
        {
            "grid_power": "sensor.netz",
            "battery": "sensor.akku",
            "thermostats": ["climate.bad", "climate.buero"],
            "setpoints": ["number.hk_tag"],
            "flag": "input_boolean.estrich",
        },
    )
    # export of 2 kW for 10 minutes → +1 K everywhere
    hass.states.async_set(
        "sensor.netz", "-2000", {"device_class": "power", "unit_of_measurement": "W"}
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert {c.data["entity_id"][0]: c.data["temperature"] for c in temps} == {
        "climate.bad": 23,
        "climate.buero": 22,
    }
    assert _values(numbers) == {"number.hk_tag": 23}
    assert hass.states.get("input_boolean.estrich").state == "on"
    # the thermostats took the new values; a second surplus does not raise again
    hass.states.async_set("climate.bad", "heat", {"temperature": 23, "max_temp": 30})
    hass.states.async_set("climate.buero", "heat", {"temperature": 22, "max_temp": 30})
    hass.states.async_set("number.hk_tag", "23", {"min": 5, "max": 30})
    temps.clear()
    numbers.clear()
    hass.states.async_set(
        "sensor.netz", "-100", {"device_class": "power", "unit_of_measurement": "W"}
    )
    await hass.async_block_till_done()
    hass.states.async_set(
        "sensor.netz", "-2500", {"device_class": "power", "unit_of_measurement": "W"}
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert not temps
    # at the end time back to the values before
    freezer.move_to(_at(16, 30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert {c.data["entity_id"]: c.data["temperature"] for c in temps} == {
        "climate.bad": 22,
        "climate.buero": 21,
    }
    assert {c.data["entity_id"]: float(c.data["value"]) for c in numbers} == {"number.hk_tag": 22.0}
    assert hass.states.get("input_boolean.estrich").state == "off"


async def test_pv_floor_heating_after_restart(
    hass: HomeAssistant, entry: MockConfigEntry, freezer
) -> None:
    """The snapshot is gone after a restart: lower by the same amount instead."""
    freezer.move_to(_at(16, 29))
    assert await async_setup_component(hass, "scene", {})
    assert await async_setup_component(
        hass,
        "input_boolean",
        {"input_boolean": {"estrich": {"name": "Estrich", "initial": True}}},
    )
    temps = async_mock_service(hass, "climate", "set_temperature")
    numbers = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("sensor.netz", "0", {"device_class": "power"})
    hass.states.async_set("climate.bad", "heat", {"temperature": 23, "max_temp": 30})
    hass.states.async_set("number.hk_tag", "23", {"min": 5, "max": 30})
    await _automation(
        hass,
        "pv_floor_heating",
        {
            "grid_power": "sensor.netz",
            "thermostats": ["climate.bad"],
            "setpoints": ["number.hk_tag"],
            "flag": "input_boolean.estrich",
        },
    )
    freezer.move_to(_at(16, 30))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert temps[-1].data["temperature"] == 22
    assert float(numbers[-1].data["value"]) == 22
    assert hass.states.get("input_boolean.estrich").state == "off"
