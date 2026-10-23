"""System and network: internet, network devices, services, status entities, power, automations."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

SOURCES = {
    k: {"push": True}
    for k in ("net.internet", "net.devices", "sys.services", "sys.power", "auto.errors")
}
SETTINGS = {
    "internet": "binary_sensor.internet",
    "devices": [{"name": "Gateway", "entity": "sensor.gateway_state"}],
    "status": [{"name": "Wechselrichter", "entity": "sensor.wr_status", "ok_states": ["OK"]}],
    "services": [{"name": "Zigbee", "domains": ["zha"], "entity": "binary_sensor.z2m_bridge"}],
}


async def _setup(hass: HomeAssistant, hass_ws_client) -> MockConfigEntry:
    hass.states.async_set("binary_sensor.internet", "on", {})
    hass.states.async_set("sensor.gateway_state", "connected", {})
    hass.states.async_set("sensor.wr_status", "OK", {})
    hass.states.async_set("binary_sensor.z2m_bridge", "on", {})
    hass.states.async_set(
        "binary_sensor.rpi_power_status", "off", {"friendly_name": "RPi Power status"}
    )
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.store.set_notify({"sources": SOURCES})
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_ui/system/settings", **SETTINGS})
    res = await client.receive_json()
    assert res["success"] and res["result"]["configured"]
    await hass.async_block_till_done()
    return entry


async def test_problems(hass: HomeAssistant, hass_ws_client, notify_calls, freezer) -> None:
    entry = await _setup(hass, hass_ws_client)
    system = entry.runtime_data.system
    zha = MockConfigEntry(domain="zha", title="ZHA", state=ConfigEntryState.SETUP_RETRY)
    zha.add_to_hass(hass)
    hass.states.async_set("binary_sensor.internet", "off", {})
    hass.states.async_set("sensor.gateway_state", "unavailable", {})
    hass.states.async_set("sensor.wr_status", "Isolationsfehler", {})
    hass.states.async_set(
        "binary_sensor.rpi_power_status", "on", {"friendly_name": "RPi Power status"}
    )
    freezer.tick(timedelta(minutes=6))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    msgs = [c.data["message"] for c in notify_calls]
    assert "Internet: getrennt" in msgs
    assert "Gateway: nicht erreichbar" in msgs
    assert "Wechselrichter: Isolationsfehler" in msgs
    assert "Zigbee: ZHA: Verbindung wird wiederholt" in msgs
    assert "Raspberry Pi: Unterspannung" in msgs
    assert hass.states.get("sensor.system_problems").state == "5"
    count = len(notify_calls)
    await system.async_evaluate()
    assert len(notify_calls) == count


async def test_automation_errors(hass: HomeAssistant, hass_ws_client, notify_calls) -> None:
    entry = await _setup(hass, hass_ws_client)
    hass.states.async_set("automation.licht", "on", {"id": "licht", "friendly_name": "Licht"})
    traces = [
        {
            "item_id": "licht",
            "run_id": "1",
            "state": "stopped",
            "script_execution": "finished",
            "timestamp": {"start": "2026-10-20T10:00:00"},
        },
    ]

    async def fake(_hass, _domain, _key):
        return traces

    with patch("homeassistant.components.trace.util.async_list_traces", fake):
        await entry.runtime_data.system.async_evaluate()
        assert notify_calls == []
        traces.append(
            {
                "item_id": "licht",
                "run_id": "2",
                "state": "stopped",
                "script_execution": "error",
                "error": "Service not found",
                "timestamp": {"start": "2026-10-21T10:00:00"},
            }
        )
        await entry.runtime_data.system.async_evaluate()
    assert notify_calls[-1].data["message"] == "Automation „Licht“: Fehler beim letzten Lauf"


async def test_access_points_back_online_restart_unifi(
    hass: HomeAssistant, hass_ws_client, notify_calls, freezer
) -> None:
    from homeassistant.helpers import device_registry as dr, entity_registry as er

    entry = await _setup(hass, hass_ws_client)
    system = entry.runtime_data.system
    # a UniFi access point that is not on the dashboard's page
    unifi = MockConfigEntry(domain="unifi")
    unifi.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=unifi.entry_id, identifiers={("unifi", "ap1")}, name="AP Flur"
    )
    reg = er.async_get(hass)
    for uid, obj in (
        ("device_state-ap1", "ap_flur_state"),
        ("device_uptime-ap1", "ap_flur_uptime"),
    ):
        reg.async_get_or_create(
            "sensor", "unifi", uid, device_id=device.id, suggested_object_id=obj, config_entry=unifi
        )
    boot = dt_util.utcnow() - timedelta(days=3)
    hass.states.async_set("sensor.ap_flur_state", "connected", {})
    hass.states.async_set("sensor.ap_flur_uptime", boot.isoformat(), {"device_class": "timestamp"})
    names = {d["name"] for d in system.network_devices()}
    assert names == {"Gateway", "AP Flur"}
    await system.async_evaluate()

    # upgrading is no alarm
    hass.states.async_set("sensor.ap_flur_state", "upgrading", {})
    freezer.tick(timedelta(minutes=6))
    await system.async_evaluate()
    assert notify_calls == []

    # offline → message, back → "wieder da", restart (boot time moved) → "neu gestartet"
    hass.states.async_set("sensor.ap_flur_state", "disconnected", {})
    freezer.tick(timedelta(minutes=12))
    await system.async_evaluate()
    assert notify_calls[-1].data["message"] == "AP Flur: disconnected"
    hass.states.async_set("sensor.ap_flur_state", "connected", {})
    hass.states.async_set(
        "sensor.ap_flur_uptime", dt_util.utcnow().isoformat(), {"device_class": "timestamp"}
    )
    await system.async_evaluate()
    msgs = [c.data["message"] for c in notify_calls]
    assert "AP Flur wieder da (12 min weg)" in msgs
    assert "AP Flur neu gestartet" in msgs

    # UniFi automatically off: only the dashboard's devices
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.system_unifi_auto"}, blocking=True
    )
    assert [d["name"] for d in system.network_devices()] == ["Gateway"]
