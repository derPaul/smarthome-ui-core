"""Energy dashboard as source: meters, power sensors and devices."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthome_ui.energy import energy_from_prefs
from homeassistant.core import HomeAssistant


def test_energy_from_prefs() -> None:
    prefs = {
        "energy_sources": [
            {
                "type": "grid",
                "flow_from": [{"stat_energy_from": "sensor.netz_bezug"}],
                "flow_to": [{"stat_energy_to": "sensor.netz_einspeisung"}],
                "power": [{"stat_rate": "sensor.netz_leistung"}],
            },
            {"type": "solar", "stat_energy_from": "sensor.pv_ertrag", "stat_rate": "sensor.pv"},
            {"type": "solar", "stat_energy_from": "external:statistic"},
            {
                "type": "battery",
                "stat_energy_from": "sensor.akku_entladen",
                "stat_energy_to": "sensor.akku_geladen",
            },
            {"type": "water", "stat_energy_from": "sensor.wasserzaehler"},
        ],
        "device_consumption": [
            {"stat_consumption": "sensor.waschmaschine_energie", "name": "Waschmaschine"},
            {"stat_consumption": "sensor.kuehlschrank_energie"},
        ],
    }
    out = energy_from_prefs(prefs)
    assert out["import"] == ["sensor.netz_bezug"]
    assert out["export"] == ["sensor.netz_einspeisung"]
    assert out["grid_power"] == ["sensor.netz_leistung"]
    assert out["pv"] == ["sensor.pv_ertrag"]  # external statistics are no entities
    assert out["pv_power"] == ["sensor.pv"]
    assert out["battery_charge"] == ["sensor.akku_geladen"]
    assert out["battery_discharge"] == ["sensor.akku_entladen"]
    assert out["water"] == ["sensor.wasserzaehler"]
    assert out["consumers"] == [
        {"entity": "sensor.waschmaschine_energie", "name": "Waschmaschine"},
        {"entity": "sensor.kuehlschrank_energie", "name": None},
    ]
    # newer format: the grid meters on the source itself
    new = energy_from_prefs(
        {
            "energy_sources": [
                {"type": "grid", "stat_energy_from": "sensor.a", "stat_energy_to": "sensor.b"}
            ]
        }
    )
    assert (new["import"], new["export"]) == (["sensor.a"], ["sensor.b"])


async def test_energy_in_get(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client) -> None:
    from homeassistant.components.energy.data import async_get_manager

    manager = await async_get_manager(hass)
    await manager.async_update(
        {
            "energy_sources": [
                {
                    "type": "solar",
                    "stat_energy_from": "sensor.pv_ertrag",
                    "config_entry_solar_forecast": None,
                }
            ]
        }
    )
    await hass.async_block_till_done()
    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_ui/get"})
    res = (await client.receive_json())["result"]
    assert "energy" in res["features"]
    assert res["energy"]["pv"] == ["sensor.pv_ertrag"]
