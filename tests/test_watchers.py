"""Watchers (batteries, plants, frost) and the vacuum station."""

from __future__ import annotations

from datetime import datetime, timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .test_planner import _setup
from .test_vacuum import _vacuum_device


async def _items(hass: HomeAssistant, entity_id: str) -> list[str]:
    res = await hass.services.async_call(
        "todo",
        "get_items",
        {},
        target={"entity_id": entity_id},
        blocking=True,
        return_response=True,
    )
    return [i["summary"] for i in res[entity_id]["items"]]


async def test_battery_and_plants(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    hass.states.async_set(
        "sensor.fenster_bad_battery",
        "9",
        {"device_class": "battery", "friendly_name": "Fenster Bad"},
    )
    hass.states.async_set("sensor.phone_battery", "50", {"device_class": "battery"})
    hass.states.async_set(
        "plant.monstera",
        "problem",
        {"friendly_name": "Monstera", "problem": "moisture low, conductivity low"},
    )
    await hass.async_block_till_done()
    assert await _items(hass, "todo.household") == ["Batterie wechseln: Fenster Bad (9 %)"]
    assert sorted(await _items(hass, "todo.garden")) == ["Monstera düngen", "Monstera gießen"]

    # checked off → gone until the battery is low again another day
    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Batterie wechseln: Fenster Bad (9 %)", "status": "completed"},
        target={"entity_id": "todo.household"},
        blocking=True,
    )
    assert await _items(hass, "todo.household") == []


async def test_frost(hass: HomeAssistant, entry: MockConfigEntry, notify_calls) -> None:
    hass.states.async_set("weather.home", "cloudy", {})
    now = dt_util.utcnow()

    async def forecasts(call: ServiceCall) -> ServiceResponse:
        return {
            "weather.home": {
                "forecast": [
                    {"datetime": (now + timedelta(hours=h)).isoformat(), "temperature": t}
                    for h, t in ((2, 4.0), (8, -2.0), (12, 1.5))
                ]
            }
        }

    hass.services.async_register(
        "weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY
    )
    runtime = entry.runtime_data
    runtime.store.set_notify({"sources": {"gd.frost": {"push": True}}})
    await runtime.watchers._async_forecast()
    await hass.async_block_till_done()
    assert runtime.watchers.frost["t"] == -2.0
    assert (await _items(hass, "todo.garden"))[0].startswith("Pflanzen vor Frost schützen")
    rule = runtime.watchers.rules()[0]
    assert rule["message"].endswith("(-2 °C) – Pflanzen schützen")
    # the notification fires at its time
    runtime.engine._tick(dt_util.utc_from_timestamp(rule["at"] / 1000) + timedelta(minutes=1))
    await hass.async_block_till_done()
    assert notify_calls[-1].data["title"] == "Frostwarnung"


async def test_vacuum_station(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to(datetime(2026, 3, 2, 12, tzinfo=dt_util.get_default_time_zone()))
    device_id = _vacuum_device(hass)
    other = next(iter(hass.config_entries.async_entries("roborock")))
    er.async_get(hass).async_get_or_create(
        "binary_sensor",
        "roborock",
        "water",
        device_id=device_id,
        config_entry=other,
        suggested_object_id="robbi_water_shortage",
    )
    hass.states.async_set(
        "binary_sensor.robbi_water_shortage", "off", {"friendly_name": "Robbi Wassermangel"}
    )
    entry = await _setup(hass, "vacuum", device_id)
    entry.runtime_data.store.set_notify({"sources": {"hh.robot": {"push": True}}})
    await hass.async_block_till_done()
    hass.states.async_set(
        "binary_sensor.robbi_water_shortage", "on", {"friendly_name": "Robbi Wassermangel"}
    )
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=2))
    entry.runtime_data.engine._tick(dt_util.utcnow())
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Robbi: Frischwasser nachfüllen"
