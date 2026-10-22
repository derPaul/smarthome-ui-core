"""Robot vacuums: subentry, room tracking, sensors, notifications and cleaning intervals."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthome_ui.const import DOMAIN
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er


def _vacuum_device(hass: HomeAssistant, room_sensor: bool = True) -> str:
    areas = ar.async_get(hass)
    for name in ("Küche", "Bad", "Flur"):
        areas.async_get_or_create(name)
    other = MockConfigEntry(domain="roborock")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("roborock", "s8")}, name="Robbi"
    )
    ents = er.async_get(hass)

    def add(domain: str, uid: str, object_id: str, state: str, attrs: dict) -> None:
        ents.async_get_or_create(
            domain,
            "roborock",
            uid,
            device_id=device.id,
            suggested_object_id=object_id,
            config_entry=other,
        )
        hass.states.async_set(f"{domain}.{object_id}", state, attrs)

    add("vacuum", "v", "robbi", "docked", {"friendly_name": "Robbi"})
    if room_sensor:
        add(
            "sensor",
            "room",
            "robbi_current_room",
            "Flur",
            {"friendly_name": "Robbi Aktueller Raum"},
        )
    add("sensor", "dock", "robbi_dock_error", "ok", {"friendly_name": "Robbi Dock error"})
    add(
        "sensor",
        "mb",
        "robbi_main_brush_time_left",
        "100",
        {"friendly_name": "Robbi Main brush time left", "unit_of_measurement": "h"},
    )
    return device.id


async def _setup(
    hass: HomeAssistant, device_id: str, sources: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Smarthome UI Core",
        subentries_data=[
            ConfigSubentryData(
                data={"device_id": device_id}, subentry_type="vacuum", title="Robbi", unique_id=None
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if sources:
        entry.runtime_data.store.set_notify({"sources": sources})
        await hass.async_block_till_done()
    return entry


async def test_add_vacuum_subentry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device_id = _vacuum_device(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "vacuum"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device_id": device_id, "name": "Saugi"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Saugi"
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert entry.runtime_data.vacuums.info()[0]["entity"] == "vacuum.robbi"


async def test_tracks_rooms(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 10:00:00")
    device_id = _vacuum_device(hass)
    entry = await _setup(hass, device_id)

    def go(state: str, room: str | None = None, minutes: float = 0) -> None:
        if minutes:
            freezer.tick(timedelta(minutes=minutes))
        if room is not None:
            hass.states.async_set("sensor.robbi_current_room", room, {"friendly_name": "x"})
        hass.states.async_set("vacuum.robbi", state, {"friendly_name": "Robbi"})

    go("cleaning", "Küche")
    await hass.async_block_till_done()
    go("cleaning", "Bad", minutes=5)  # 5 min kitchen
    await hass.async_block_till_done()
    go("cleaning", "Flur", minutes=1)  # 1 min bathroom → does not count
    await hass.async_block_till_done()
    go("returning", minutes=3)  # 3 min hall
    await hass.async_block_till_done()
    go("docked", minutes=1)
    await hass.async_block_till_done()

    cleaned = entry.runtime_data.store.data["data"]["household"]["cleaned"]
    kitchen = ar.async_get(hass).async_get_area_by_name("Küche").id
    hall = ar.async_get(hass).async_get_area_by_name("Flur").id
    bath = ar.async_get(hass).async_get_area_by_name("Bad").id
    assert set(cleaned) == {kitchen, hall}
    assert bath not in cleaned

    state = hass.states.get("sensor.household_kuche_last_vacuumed")
    assert state is not None and state.state != "unknown"
    entity = er.async_get(hass).async_get("sensor.household_kuche_last_vacuumed")
    assert entity.device_id == device_id


async def test_dashboard_run_without_room_sensor(
    hass: HomeAssistant, notify_calls, freezer
) -> None:
    freezer.move_to("2026-03-02 10:00:00")
    device_id = _vacuum_device(hass, room_sensor=False)
    entry = await _setup(hass, device_id)
    bath = ar.async_get(hass).async_get_area_by_name("Bad").id
    store = entry.runtime_data.store
    store.set_data({"household": {"runs": [{"t": 1772445600000, "areas": [bath]}]}})
    hass.states.async_set("vacuum.robbi", "cleaning", {"friendly_name": "Robbi"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=12))
    hass.states.async_set("vacuum.robbi", "docked", {"friendly_name": "Robbi"})
    await hass.async_block_till_done()
    assert list(store.data["data"]["household"]["cleaned"]) == [bath]


async def test_vacuum_notifications(hass: HomeAssistant, notify_calls) -> None:
    device_id = _vacuum_device(hass)
    await _setup(hass, device_id, {"hh.robot": {"push": True}})
    hass.states.async_set("vacuum.robbi", "error", {"friendly_name": "Robbi"})
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Robbi: Fehler"
    # main brush: life 300 h → below 30 h
    hass.states.async_set(
        "sensor.robbi_main_brush_time_left",
        "20",
        {"friendly_name": "x", "unit_of_measurement": "h"},
    )
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Robbi: Hauptbürste reinigen/wechseln"


async def test_cleaning_interval_numbers(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    store = entry.runtime_data.store
    store.set_tasks(
        [
            {
                "key": "clean:kuche",
                "name": "Küche putzen",
                "area": "Küche",
                "interval": 2,
                "last": 1000,
            }
        ]
    )
    await hass.async_block_till_done()
    assert hass.states.get("number.household_kuche_clean_interval").state == "2"
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.household_kuche_clean_interval", "value": 4},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert store.data["cleaning"]["intervals"] == {"kuche": 4}
    assert store.data["tasks"][0]["interval"] == 4
    assert hass.states.get("number.household_kuche_clean_interval").state == "4"

    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "smarthome_ui/cleaning/settings", "intervals": {"kuche": 3, "bad": 0}}
    )
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"] == {"intervals": {"kuche": 3, "bad": 0}, "configured": True}


async def test_entities_on_vacuum_device(hass: HomeAssistant, notify_calls) -> None:
    """The planner (and room sensors once rooms are known) go onto the vacuum's own device."""
    device_id = _vacuum_device(hass)
    await _setup(hass, device_id)
    registry = er.async_get(hass)
    ours = [e for e in registry.entities.values() if e.platform == "smarthome_ui" and e.device_id]
    on_vacuum = [e for e in ours if e.device_id == device_id]
    assert any(e.entity_id.endswith("_planner") for e in on_vacuum)
    assert not [d for d in dr.async_get(hass).devices.values() if d.name == "Smarthome UI Core"]
