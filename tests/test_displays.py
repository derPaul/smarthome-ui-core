"""Displays: open dashboards become devices with state and controls."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.setup import async_setup_component

OPTIONS = {
    "page": ["Übersicht", "Garten"],
    "theme": ["dark", "sand", "clay", "auto"],
    "season": ["auto", "off", "herbst"],
    "sounds": ["off", "quiet", "normal"],
    "performance": ["auto", "full", "lite"],
}
STATE = {
    "page": "Übersicht",
    "theme": "dark",
    "season": "auto",
    "sounds": "quiet",
    "performance": "auto",
    "standby": False,
}


async def _connect(client, display="tab1", name="Wandtablet Flur"):
    await client.send_json_auto_id(
        {
            "type": "smarthome_ui/display/connect",
            "display": display,
            "name": name,
            "user": "Paul",
            "state": STATE,
            "options": OPTIONS,
        }
    )
    msg = await client.receive_json()
    assert msg["success"], msg
    return msg["id"]


async def test_display_entities_and_commands(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    client = await hass_ws_client(hass)
    sub = await _connect(client)
    await hass.async_block_till_done()

    assert hass.states.get("binary_sensor.display_wandtablet_flur_online").state == "on"
    assert hass.states.get("sensor.display_wandtablet_flur_user").state == "Paul"
    page = hass.states.get("select.display_wandtablet_flur_page")
    assert page.state == "Übersicht"
    assert page.attributes["options"] == ["Übersicht", "Garten"]
    assert hass.states.get("select.display_wandtablet_flur_theme").state == "dark"
    assert hass.states.get("switch.display_wandtablet_flur_standby").state == "off"

    devices = dr.async_get(hass)
    entity = er.async_get(hass).async_get("select.display_wandtablet_flur_page")
    assert devices.async_get(entity.device_id).name == "Wandtablet Flur"

    # change from Home Assistant → command to the dashboard
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.display_wandtablet_flur_page", "option": "Garten"},
        blocking=True,
    )
    msg = await client.receive_json()
    assert msg["id"] == sub
    assert msg["event"] == {"type": "set", "key": "page", "value": "Garten"}
    assert hass.states.get("select.display_wandtablet_flur_page").state == "Garten"

    await hass.services.async_call(
        "switch", "turn_on", {"entity_id": "switch.display_wandtablet_flur_standby"}, blocking=True
    )
    assert (await client.receive_json())["event"] == {
        "type": "set",
        "key": "standby",
        "value": True,
    }
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.display_wandtablet_flur_reload"}, blocking=True
    )
    assert (await client.receive_json())["event"] == {"type": "reload"}

    # the dashboard reports a change made on the tablet
    await client.send_json_auto_id(
        {"type": "smarthome_ui/display/update", "display": "tab1", "state": {"theme": "clay"}}
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get("select.display_wandtablet_flur_theme").state == "clay"

    # closing the dashboard → offline, commands fail
    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub})
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.display_wandtablet_flur_online").state == "off"
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "button", "press", {"entity_id": "button.display_wandtablet_flur_reload"}, blocking=True
        )


async def test_display_survives_restart_and_can_be_removed(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    assert await async_setup_component(hass, "config", {})
    client = await hass_ws_client(hass)
    sub = await _connect(client, "tab2", "iPad Küche")
    await hass.async_block_till_done()

    devices = dr.async_get(hass)
    device = devices.async_get_device(
        identifiers={("smarthome_ui", f"{entry.entry_id}_display_tab2")}
    )
    # open → cannot be removed
    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": device.id,
        }
    )
    assert not (await client.receive_json())["success"]

    await client.send_json_auto_id({"type": "unsubscribe_events", "subscription": sub})
    assert (await client.receive_json())["success"]

    # reload: the display is still there (offline)
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.display_ipad_kuche_online").state == "off"
    assert hass.states.get("select.display_ipad_kuche_theme").state == "dark"

    await client.send_json_auto_id(
        {
            "type": "config/device_registry/remove_config_entry",
            "config_entry_id": entry.entry_id,
            "device_id": device.id,
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    assert "tab2" not in entry.runtime_data.displays.displays
    assert hass.states.get("binary_sensor.display_ipad_kuche_online") is None


async def test_reload_asks_dashboards_to_reconnect(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    client = await hass_ws_client(hass)
    sub = await _connect(client, "tab3", "Handy")
    assert await hass.config_entries.async_reload(entry.entry_id)
    msg = await client.receive_json()
    assert msg["id"] == sub
    assert msg["event"] == {"type": "reconnect"}
