"""Setup, config flow, websocket data API and actions."""

from __future__ import annotations

from datetime import date

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthome_ui.const import DOMAIN
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import ServiceValidationError


async def test_config_flow(hass: HomeAssistant) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.CREATE_ENTRY

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"


async def test_data_and_done(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client) -> None:
    client = await hass_ws_client(hass)
    await client.send_json_auto_id({"type": "smarthome_ui/get"})
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["migrated"] is False
    assert msg["result"]["data"]["done"] == {}

    await client.send_json_auto_id({"type": "smarthome_ui/subscribe"})
    assert (await client.receive_json())["success"]

    old = {
        "done": {"clean:bad": {"t": 1000, "by": "hand"}},
        "done_log": [{"k": "clean:bad", "t": 1000, "who": "", "label": "Bad geputzt"}],
        "household": {"runs": [{"t": 900, "areas": ["kuche"]}]},
        "mow_log": {"Vorgarten": {"last": 800}},
    }
    await client.send_json_auto_id({"type": "smarthome_ui/data/set", "data": old})
    while (msg := await client.receive_json())["type"] == "event":
        pass
    assert msg["success"]
    assert entry.runtime_data.store.data["data"]["mow_log"] == {"Vorgarten": {"last": 800}}

    await client.send_json_auto_id(
        {
            "type": "smarthome_ui/done",
            "changes": {"clean:kuche": {"t": 2000, "by": "hand", "who": "Alex"}, "clean:bad": None},
            "log": [{"k": "clean:kuche", "t": 2000, "who": "Alex", "label": "Küche geputzt"}],
            "unlog": ["clean:bad|1000"],
        }
    )
    event = None
    while True:
        msg = await client.receive_json()
        if msg["type"] == "event":
            event = msg["event"]
        elif msg["type"] == "result":
            break
    if event is None:
        event = (await client.receive_json())["event"]
    assert event["data"]["done"] == {"clean:kuche": {"t": 2000, "by": "hand", "who": "Alex"}}
    assert [e["k"] for e in event["data"]["done_log"]] == ["clean:kuche"]

    await client.send_json_auto_id(
        {"type": "smarthome_ui/data/set", "data": {"mow_log": {"Hinten": {"last": 5}}}}
    )
    while (msg := await client.receive_json())["type"] == "event":
        pass
    store = entry.runtime_data.store
    assert store.data["data"]["mow_log"] == {"Hinten": {"last": 5}}


async def test_actions(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    store = entry.runtime_data.store
    store.set_tasks([{"key": "clean:bad", "name": "Bad putzen", "interval": 7}])

    await hass.services.async_call(DOMAIN, "task_done", {"task": "bad putzen"}, blocking=True)
    assert store.done["clean:bad"]["t"] > 0
    assert store.data["data"]["done_log"][-1]["label"] == "Bad putzen"

    await hass.services.async_call(DOMAIN, "task_reopen", {"task": "clean:bad"}, blocking=True)
    assert store.done["clean:bad"] == {"t": 0}

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "task_done", {"task": "Gibt es nicht"}, blocking=True
        )

    await hass.services.async_call(DOMAIN, "waste_ack", {"date": date(2026, 10, 23)}, blocking=True)
    assert store.done["waste:2026-10-23"]["by"] == "hand"
    await hass.services.async_call(DOMAIN, "waste_skip", {"date": "2026-10-30"}, blocking=True)
    assert store.done["waste:2026-10-30"]["by"] == "skip"

    await hass.services.async_call(DOMAIN, "area_mowed", {"area": "Vorgarten"}, blocking=True)
    assert store.data["data"]["mow_log"]["Vorgarten"]["mode"] == "von Hand"


async def test_data_survives_reload(
    hass: HomeAssistant, entry: MockConfigEntry, hass_storage
) -> None:
    entry.runtime_data.store.mark_done("clean:bad")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.store.done["clean:bad"]["t"] > 0
