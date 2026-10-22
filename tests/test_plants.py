"""Plants: watering noticed, forecast, right list, rain, sensor offline, fertilizing calendar."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er


def _plant(hass: HomeAssistant, name: str, area: str, moisture: float, problem: str = "") -> None:
    """A plant like Plant Monitor creates it: device, plant entity, moisture sensor, limit."""
    slug = name.lower()
    other = MockConfigEntry(domain="plant")
    other.add_to_hass(hass)
    area_id = ar.async_get(hass).async_get_or_create(area).id
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("plant", slug)}, name=name
    )
    dr.async_get(hass).async_update_device(device.id, area_id=area_id)
    registry = er.async_get(hass)
    for domain, object_id, state, attrs in (
        (
            "plant",
            slug,
            "problem" if problem else "ok",
            {"friendly_name": name, "problem": problem},
        ),
        ("sensor", f"{slug}_soil_moisture", str(moisture), {"device_class": "moisture"}),
        (
            "number",
            f"{slug}_min_soil_moisture",
            "20",
            {"friendly_name": f"{name} Min soil moisture"},
        ),
    ):
        registry.async_get_or_create(
            domain,
            "plant",
            object_id,
            device_id=device.id,
            suggested_object_id=object_id,
            config_entry=other,
        )
        hass.states.async_set(f"{domain}.{object_id}", state, attrs)


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


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_watering_forecast_and_list(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-06-01 08:00:00")
    _plant(hass, "Monstera", "Wohnzimmer", 30)
    entry = await _setup(hass)
    plants = entry.runtime_data.plants
    monstera = plants.plants["plant.monstera"]
    assert not monstera.outdoor
    assert monstera.min_moisture() == 20

    def moisture(value: float) -> None:
        hass.states.async_set(
            "sensor.monstera_soil_moisture", str(value), {"device_class": "moisture"}
        )

    # watered: moisture jumps
    freezer.tick(timedelta(minutes=5))
    moisture(52)
    await hass.async_block_till_done()
    assert monstera.last_watered()[1] == "auto"
    assert hass.states.get("sensor.household_monstera_last_watered").attributes["by"] == "auto"

    # drying 1 point per hour for a day → forecast
    for value in range(51, 26, -1):
        freezer.tick(timedelta(hours=1))
        moisture(value)
        await hass.async_block_till_done()
    rate = monstera.drying_rate()
    assert -25 < rate < -23
    state = hass.states.get("sensor.household_monstera_next_watering")
    assert state.attributes["drying_per_day"] > 23
    nxt = monstera.next_watering()
    hours = (nxt - int(dt_now(freezer))) / 3_600_000
    assert 5 < hours < 8
    # due later today → already in the list for today
    assert await _items(hass, "todo.household") == ["Monstera gießen"]

    # at the limit → task in "Haushalt" (indoor plant)
    for value in (24, 22, 19):
        freezer.tick(timedelta(hours=2))
        moisture(value)
        await hass.async_block_till_done()
    assert await _items(hass, "todo.household") == ["Monstera gießen"]
    assert await _items(hass, "todo.garden") == []
    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Monstera gießen", "status": "completed"},
        target={"entity_id": "todo.household"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert monstera.last_watered()[1] == "hand"
    assert await _items(hass, "todo.household") == []


def dt_now(freezer) -> float:
    from homeassistant.util import dt as dt_util

    return dt_util.utcnow().timestamp() * 1000


async def test_outdoor_rain_and_offline(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-06-01 08:00:00")
    _plant(hass, "Tomate", "Balkon", 12, problem="moisture low")
    entry = await _setup(hass)
    plants = entry.runtime_data.plants
    assert plants.plants["plant.tomate"].outdoor
    assert await _items(hass, "todo.garden") == ["Tomate gießen"]
    assert hass.states.get("button.garden_tomate_watered") is not None

    hass.states.async_set("weather.home", "rainy", {})
    plants._tick()
    await hass.async_block_till_done()
    assert plants.rain() == "rained"
    assert await _items(hass, "todo.garden") == []

    # 13 hours later dry again – and the sensor sent nothing for a day
    hass.states.async_set("weather.home", "sunny", {})
    freezer.tick(timedelta(hours=25))
    plants._tick()
    await hass.async_block_till_done()
    items = await _items(hass, "todo.garden")
    assert "Tomate gießen" in items
    assert any(i.startswith("Sensor Tomate prüfen") for i in items)


async def test_fertilize_by_calendar(hass: HomeAssistant, freezer) -> None:
    freezer.move_to("2026-05-10 08:00:00")
    _plant(hass, "Ficus", "Büro", 40)
    entry = await _setup(hass)
    plants = entry.runtime_data.plants
    ficus = plants.plants["plant.ficus"]
    assert not ficus.has_conductivity
    plants.set_settings("plant.ficus", {"every": 2, "from": 3, "to": 9})
    await hass.async_block_till_done()
    assert await _items(hass, "todo.household") == ["Ficus düngen"]
    await hass.services.async_call(
        "todo",
        "update_item",
        {"item": "Ficus düngen", "status": "completed"},
        target={"entity_id": "todo.household"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert await _items(hass, "todo.household") == []
    due = ficus.feed_due()
    from homeassistant.util import dt as dt_util

    assert (
        dt_util.as_local(dt_util.utc_from_timestamp(due / 1000)).date().isoformat() == "2026-05-24"
    )
    # outside the months: nothing
    freezer.move_to("2026-11-10 08:00:00")
    assert ficus.feed_due() is None


async def test_entities_on_plant_device_and_duplicates_removed(hass: HomeAssistant) -> None:
    """Our entities go onto the plant's own device; empty copies named after Core are removed."""
    _plant(hass, "Monstera", "Wohnzimmer", 30)
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    devices = dr.async_get(hass)
    # what newer Home Assistant versions created for our entities so far
    copy = devices.async_get_or_create(
        config_entry_id=entry.entry_id, identifiers={("plant", "copy_of_monstera")}
    )
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    assert devices.async_get(copy.id) is None
    plant_device = devices.async_get_device(identifiers={("plant", "monstera")})
    for entity_id in (
        "sensor.household_monstera_last_watered",
        "sensor.household_monstera_next_watering",
        "button.household_monstera_watered",
    ):
        assert er.async_get(hass).async_get(entity_id).device_id == plant_device.id
    assert not [d for d in devices.devices.values() if d.name == "Smarthome UI Core"]
    assert hass.states.get("sensor.household_monstera_last_watered").name == (
        "Monstera Last watered"
    )
