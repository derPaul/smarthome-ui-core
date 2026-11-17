"""History per device: shorter or longer recorder history for chosen devices."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
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
    hass.states.async_set("sensor.plug_p", "5")
    hass.states.async_set("sensor.plug_e", "1")
    hass.states.async_set("sensor.other", "1")
    rec = SimpleNamespace(keep_days=10)
    with patch("custom_components.smarthome_ui.history._recorder", return_value=rec):
        entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        purge = async_mock_service(hass, "recorder", "purge_entities")
        client = await hass_ws_client(hass)

        async def send(msg: dict) -> dict:
            await client.send_json_auto_id(msg)
            res = await client.receive_json()
            assert res["success"], res
            return res["result"]

        async def run() -> list:
            purge.clear()
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=40))
            await hass.async_block_till_done()
            return sorted((c.data["keep_days"], tuple(sorted(c.data["entity_id"]))) for c in purge)

        # shorter: only this device is purged
        res = await send({"type": "smarthome_ui/history/set", "device_id": dev.id, "days": 3})
        assert res["devices"] == {dev.id: 3}
        assert res["default"] == 10 and res["keep"] == 10
        assert await run() == [(3, ("sensor.plug_e", "sensor.plug_p"))]
        assert rec.keep_days == 10

        # longer: recorder keeps 60 days, everything else is purged to the default
        res = await send({"type": "smarthome_ui/history/set", "device_id": dev.id, "days": 60})
        assert res["keep"] == 60 and rec.keep_days == 60
        groups = dict(await run())
        assert 60 not in groups
        assert "sensor.other" in groups[10] and "sensor.plug_p" not in groups[10]

        # own default of 5 days for all others
        res = await send({"type": "smarthome_ui/history/default", "days": 5})
        assert res["default"] == 5 and res["configured"] == 10
        groups = dict(await run())
        assert "sensor.other" in groups[5]

        # back: device as default, default as configured
        await send({"type": "smarthome_ui/history/set", "device_id": dev.id, "days": None})
        res = await send({"type": "smarthome_ui/history/default", "days": None})
        assert res["devices"] == {} and res["default"] == 10 and rec.keep_days == 10

        await send({"type": "smarthome_ui/get"})
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert rec.keep_days == 10
