"""Household appliances: subentry, runs, unloading, notifications, fridge."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthome_ui.appliance import detect_kind
from custom_components.smarthome_ui.const import DOMAIN
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er


def _device(hass: HomeAssistant, name: str, entities: list[tuple]) -> str:
    other = MockConfigEntry(domain="home_connect")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("home_connect", name)}, name=name
    )
    registry = er.async_get(hass)
    for domain, object_id, state, attrs in entities:
        registry.async_get_or_create(
            domain,
            "home_connect",
            object_id,
            device_id=device.id,
            suggested_object_id=object_id,
            config_entry=other,
        )
        hass.states.async_set(f"{domain}.{object_id}", state, attrs)
    return device.id


def _dishwasher(hass: HomeAssistant) -> str:
    return _device(
        hass,
        "Geschirrspüler",
        [
            ("sensor", "spueler_operation_state", "ready", {"friendly_name": "Betriebszustand"}),
            (
                "sensor",
                "spueler_energy",
                "10.0",
                {"device_class": "energy", "unit_of_measurement": "kWh"},
            ),
            ("select", "spueler_program", "eco_50", {"friendly_name": "Programm"}),
            ("binary_sensor", "spueler_door", "off", {"device_class": "door"}),
        ],
    )


async def _setup(
    hass: HomeAssistant, device_id: str, title: str, data: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Smarthome UI Core",
        subentries_data=[
            ConfigSubentryData(
                data={"device_id": device_id, "kind": "auto", **(data or {})},
                subentry_type="appliance",
                title=title,
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.store.set_notify(
        {"sources": {"hh.dish": {"push": True}, "hh.fridge": {"push": True}}}
    )
    await hass.async_block_till_done()
    return entry


def test_detect_kind() -> None:
    assert detect_kind("Geschirrspüler SMV4") == "dishwasher"
    assert detect_kind("Waschtrockner") == "washer"
    assert detect_kind("Trockner") == "dryer"
    assert detect_kind("Kühl-Gefrier-Kombi") == "fridge"
    assert detect_kind("Kaffeemaschine") == "other"


async def test_add_and_reconfigure(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device_id = _dishwasher(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "appliance"), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device_id": device_id, "kind": "auto"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Geschirrspüler"
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    info = entry.runtime_data.appliances.info()[0]
    assert info["kind"] == "dishwasher"
    assert hass.states.get("sensor.household_geschirrspuler_status").state == "off"
    assert hass.states.get("binary_sensor.household_geschirrspuler_to_unload").state == "off"
    assert hass.states.get("button.household_geschirrspuler_unloaded") is not None

    subentry = next(iter(entry.subentries.values()))
    result = await entry.start_subentry_reconfigure_flow(hass, subentry.subentry_id)
    assert "reminder" in result["data_schema"].schema
    assert "max_temp" not in result["data_schema"].schema
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {"device_id": device_id, "kind": "dishwasher", "reminder": 45, "solar_min": 1200},
    )
    assert result["type"] is FlowResultType.ABORT
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert entry.runtime_data.appliances.info()[0]["options"]["reminder"] == 45


async def test_run_finish_unload(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 10:00:00")
    device_id = _dishwasher(hass)
    entry = await _setup(hass, device_id, "Spüli")
    manager = entry.runtime_data.appliances
    tracker = next(iter(manager.trackers.values()))

    hass.states.async_set("sensor.spueler_operation_state", "run", {})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_spuli_status").state == "running"
    freezer.tick(timedelta(minutes=60))
    hass.states.async_set(
        "sensor.spueler_energy", "11.1", {"device_class": "energy", "unit_of_measurement": "kWh"}
    )
    hass.states.async_set("sensor.spueler_operation_state", "finished", {})
    await hass.async_block_till_done()

    last = tracker.last_run()
    assert last["minutes"] == 60
    assert last["kwh"] == 1.1
    assert last["program"] == "eco_50"
    assert hass.states.get("binary_sensor.household_spuli_to_unload").state == "on"
    assert hass.states.get("sensor.household_spuli_last_run").attributes["minutes"] == 60
    assert [c.data["message"] for c in notify_calls] == ["Spüli fertig"]
    assert [t["name"] for t in entry.runtime_data.tasks()] == ["Spüli ausräumen"]
    todo = hass.states.get("todo.household")
    assert todo.state == "1"

    # reminder after 30 minutes (once)
    freezer.tick(timedelta(minutes=31))
    manager._tick()
    manager._tick()
    await hass.async_block_till_done()
    assert len(notify_calls) == 2
    assert "bitte ausräumen" in notify_calls[1].data["message"]

    await hass.services.async_call(
        "button", "press", {"entity_id": "button.household_spuli_unloaded"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.household_spuli_to_unload").state == "off"
    assert entry.runtime_data.tasks() == []
    assert entry.runtime_data.store.done[f"appl:device:{device_id}"]["t"]


async def test_door_and_todo_clear_unload(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 10:00:00")
    device_id = _dishwasher(hass)
    entry = await _setup(hass, device_id, "Spüli")
    tracker = next(iter(entry.runtime_data.appliances.trackers.values()))

    async def run() -> None:
        hass.states.async_set("sensor.spueler_operation_state", "run", {})
        await hass.async_block_till_done()
        freezer.tick(timedelta(minutes=40))
        hass.states.async_set("sensor.spueler_operation_state", "finished", {})
        await hass.async_block_till_done()

    await run()
    assert tracker.to_unload()
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set("binary_sensor.spueler_door", "on", {"device_class": "door"})
    await hass.async_block_till_done()
    assert not tracker.to_unload()
    hass.states.async_set("binary_sensor.spueler_door", "off", {"device_class": "door"})

    # a short "run" (rinse) does not count
    hass.states.async_set("sensor.spueler_operation_state", "run", {})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=2))
    hass.states.async_set("sensor.spueler_operation_state", "ready", {})
    await hass.async_block_till_done()
    assert not tracker.to_unload()
    assert tracker.last_run()["minutes"] == 40

    await run()
    assert tracker.to_unload()
    await hass.services.async_call(
        "todo",
        "update_item",
        {"entity_id": "todo.household", "item": "Spüli ausräumen", "status": "completed"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert not tracker.to_unload()


async def test_finished_while_down_no_push(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 10:00:00")
    device_id = _dishwasher(hass)
    hass.states.async_set("sensor.spueler_operation_state", "finished", {})
    freezer.tick(timedelta(minutes=5))
    entry = await _setup(hass, device_id, "Spüli")
    tracker = next(iter(entry.runtime_data.appliances.trackers.values()))
    assert tracker.to_unload()
    assert notify_calls == []


async def test_error_and_solar(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 10:00:00")
    device_id = _dishwasher(hass)
    hass.states.async_set("sensor.pv_surplus", "500", {"unit_of_measurement": "W"})
    entry = await _setup(
        hass, device_id, "Spüli", {"solar_entity": "sensor.pv_surplus", "solar_min": 1000}
    )
    hass.states.async_set("sensor.spueler_operation_state", "delayed_start", {})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_spuli_status").state == "waiting"
    assert notify_calls == []
    hass.states.async_set("sensor.pv_surplus", "1.5", {"unit_of_measurement": "kW"})
    entry.runtime_data.appliances._tick()
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Sonnenstrom übrig – Spüli jetzt starten?"
    hass.states.async_set("sensor.spueler_operation_state", "error", {})
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Spüli: Störung"
    assert len(notify_calls) == 2


async def test_fridge(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-03-02 10:00:00")
    device_id = _device(
        hass,
        "Kühlschrank",
        [
            ("sensor", "kuehl_temp", "5", {"device_class": "temperature"}),
            (
                "sensor",
                "kuehl_freezer_temp",
                "-18",
                {"device_class": "temperature", "friendly_name": "Gefrierteil"},
            ),
            ("binary_sensor", "kuehl_door", "off", {"device_class": "door"}),
            ("binary_sensor", "kuehl_door_alarm", "off", {"device_class": "problem"}),
        ],
    )
    entry = await _setup(hass, device_id, "Kühli")
    manager = entry.runtime_data.appliances
    assert hass.states.get("sensor.household_kuhli_status").state == "ok"
    assert hass.states.get("binary_sensor.household_kuhli_to_unload") is None
    assert hass.states.get("sensor.household_kuhli_last_run") is None

    hass.states.async_set("binary_sensor.kuehl_door", "on", {"device_class": "door"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_kuhli_status").state == "door_open"
    freezer.tick(timedelta(minutes=4))
    manager._tick()
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Kühli: Tür seit 4 min offen"
    hass.states.async_set("binary_sensor.kuehl_door", "off", {"device_class": "door"})
    hass.states.async_set("sensor.kuehl_temp", "10", {"device_class": "temperature"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_kuhli_status").state == "warm"
    assert len(notify_calls) == 1
    freezer.tick(timedelta(minutes=16))
    manager._tick()
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Kühli: Kühlteil zu warm (10 °C)"
    hass.states.async_set("binary_sensor.kuehl_door_alarm", "on", {"device_class": "problem"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_kuhli_status").state == "alarm"
    assert notify_calls[-1].data["message"] == "Kühli: Alarm"


async def test_fridge_entities_on_appliance_device(hass: HomeAssistant, notify_calls) -> None:
    """Core's entities go onto the fridge's own device – no device named after Core."""
    device_id = _device(
        hass,
        "Kühlschrank",
        [("sensor", "kuehl_temp", "5", {"device_class": "temperature"})],
    )
    await _setup(hass, device_id, "Kühli")
    assert er.async_get(hass).async_get("sensor.household_kuhli_status").device_id == device_id
    assert not [d for d in dr.async_get(hass).devices.values() if d.name == "Smarthome UI Core"]
