"""System and network: internet, network devices, services, status entities, power, automations."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

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
