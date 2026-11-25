"""Watcher: database report (states per entity) and its actions."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(recorder_mock, enable_custom_integrations):
    """The recorder has to start before Home Assistant (and the custom integrations)."""
    return


async def test_database_report(hass: HomeAssistant, hass_ws_client, notify_calls) -> None:
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    reg = er.async_get(hass)
    chatty = reg.async_get_or_create(
        "sensor", "test", "lq", suggested_object_id="flur_lampe_linkquality"
    )
    for v in range(30):
        hass.states.async_set(chatty.entity_id, str(v))
    for v in range(10):
        hass.states.async_set("sensor.temperatur", str(20 + v / 10))
    await async_wait_recording_done(hass)
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_ui/guard/scan"})
    res = await client.receive_json()
    assert res["success"], res
    db = res["result"]["db"]
    top = {t["entity_id"]: t for t in db["top"]}
    assert db["dialect"] == "sqlite" and db["size"] > 0 and db["total"] >= 40
    assert top[chatty.entity_id]["count"] == 30 and top["sensor.temperatur"]["count"] == 10
    assert db["top"][0]["entity_id"] == chatty.entity_id
    assert top[chatty.entity_id]["suggest"] == "disable"
    assert top["sensor.temperatur"]["suggest"] == "short"  # not in the registry: only shorter
    # keep the temperature 1 day, switch the link quality off
    await client.send_json(
        {
            "id": 2,
            "type": "smarthome_ui/guard/db_action",
            "entity_id": "sensor.temperatur",
            "action": "short",
        }
    )
    res = await client.receive_json()
    assert res["success"]
    assert runtime.history.entities == {"sensor.temperatur": 1}
    assert (
        next(t for t in res["result"]["db"]["top"] if t["entity_id"] == "sensor.temperatur")["keep"]
        == 1
    )
    await client.send_json(
        {
            "id": 3,
            "type": "smarthome_ui/guard/db_action",
            "entity_id": chatty.entity_id,
            "action": "disable",
        }
    )
    res = await client.receive_json()
    assert res["success"] and reg.async_get(chatty.entity_id).disabled_by is not None
    # shorter history: the entity is purged with 1 day (and not with the default)
    runtime.history.store.data["history_default"] = 5
    runtime.history.configured = 10
    groups = runtime.history.groups()
    assert "sensor.temperatur" in groups[1] and "sensor.temperatur" not in groups.get(5, [])
