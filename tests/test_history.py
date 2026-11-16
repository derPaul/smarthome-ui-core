"""History per device: shorter recorder history for chosen devices."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util


async def test_history_per_device(hass: HomeAssistant, hass_ws_client) -> None:
    other = MockConfigEntry(domain="shelly")
    other.add_to_hass(hass)
    dev = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("shelly", "plug1")}, name="Steckdose"
    )
    ents = er.async_get(hass)
    for uid in ("p", "e"):
        ents.async_get_or_create(
            "sensor",
            "shelly",
            uid,
            device_id=dev.id,
            suggested_object_id=f"plug_{uid}",
            config_entry=other,
        )
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    purge = async_mock_service(hass, "recorder", "purge_entities")
    runtime = entry.runtime_data

    client = await hass_ws_client(hass)
    with patch.object(type(runtime.history), "default_days", return_value=10):
        await client.send_json_auto_id(
            {"type": "smarthome_ui/history/set", "device_id": dev.id, "days": 3}
        )
        msg = await client.receive_json()
        assert msg["success"], msg
        assert msg["result"]["devices"] == {dev.id: 3}
        assert msg["result"]["default"] == 10

        # purged a little after the change
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
        await hass.async_block_till_done()
        assert len(purge) == 1
        assert purge[0].data["keep_days"] == 3
        assert sorted(purge[0].data["entity_id"]) == ["sensor.plug_e", "sensor.plug_p"]
        assert runtime.store.data["history_run"]["entities"] == 2

        # as long as the default (or longer): back to the default
        await client.send_json_auto_id(
            {"type": "smarthome_ui/history/set", "device_id": dev.id, "days": 14}
        )
        msg = await client.receive_json()
        assert msg["result"]["devices"] == {}

    await client.send_json_auto_id({"type": "smarthome_ui/get"})
    msg = await client.receive_json()
    assert "history_keep" in msg["result"]["features"]
    assert "history" in msg["result"]
