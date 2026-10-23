"""House technology: faults, heating pressure, softener, water watch, cycling, base load."""

from __future__ import annotations

from datetime import datetime, timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

SOURCES = {"ht.heating": {"push": True}, "ht.energy": {"push": True}, "ht.water": {"push": True}}
SETTINGS = {
    "heatpump_error": "sensor.wp_fehler",
    "inverter_error": "sensor.wr_status",
    "inverter_error_ok": ["OK", "Normal"],
    "reach": {"Batterie": "sensor.batterie_soc"},
    "pressure": "sensor.heizungsdruck",
    "salt": "sensor.syr_salz",
    "syr": {"Alarm": "sensor.syr_alarm"},
    "flow": "sensor.syr_durchfluss",
    "compressor": "binary_sensor.verdichter",
    "power": {"house": "sensor.hausverbrauch"},
}


def _states(hass: HomeAssistant) -> None:
    for eid, state in (
        ("sensor.wp_fehler", "Kein Fehler"),
        ("sensor.wr_status", "OK"),
        ("sensor.batterie_soc", "80"),
        ("sensor.heizungsdruck", "1.8"),
        ("sensor.syr_salz", "20"),
        ("sensor.syr_alarm", "Kein Alarm"),
        ("sensor.syr_durchfluss", "0"),
        ("binary_sensor.verdichter", "off"),
        ("sensor.hausverbrauch", "300"),
    ):
        hass.states.async_set(eid, state, {})
    hass.states.async_set("valve.hauptwasser", "open", {"friendly_name": "Hauptwasser"})


async def _setup(hass: HomeAssistant, hass_ws_client, client=None) -> MockConfigEntry:
    _states(hass)
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.store.set_notify({"sources": SOURCES})
    client = client or await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "smarthome_ui/technik/settings", **SETTINGS})
    res = await client.receive_json()
    assert res["success"] and res["result"]["configured"]
    await hass.async_block_till_done()
    return entry


def _messages(calls) -> list[str]:
    return [c.data["message"] for c in calls]


async def test_faults_pressure_softener(hass: HomeAssistant, hass_ws_client, notify_calls) -> None:
    entry = await _setup(hass, hass_ws_client)
    technik = entry.runtime_data.technik
    assert notify_calls == []
    hass.states.async_set("sensor.wp_fehler", "Fehler 712 Hochdruck", {})
    hass.states.async_set("sensor.wr_status", "Isolationsfehler", {})
    hass.states.async_set("sensor.heizungsdruck", "0.9", {})
    hass.states.async_set("sensor.syr_salz", "4", {})
    hass.states.async_set("sensor.syr_alarm", "Salzmangel", {})
    technik.evaluate()
    await hass.async_block_till_done()
    msgs = _messages(notify_calls)
    assert "Wärmepumpe: Fehler 712 Hochdruck" in msgs
    assert "Wechselrichter: Isolationsfehler" in msgs
    assert "Heizungswasser nachfüllen (0,90 bar)" in msgs
    assert "Salz nachfüllen (4 kg)" in msgs
    assert "Entkalker-Alarm: Salzmangel" in msgs
    pressure = next(c for c in notify_calls if "Heizungswasser" in c.data["message"])
    assert pressure.data["data"]["push"]["interruption-level"] == "time-sensitive"  # < 1 bar
    assert hass.states.get("sensor.technik_hints").state == "5"
    count = len(notify_calls)
    technik.evaluate()
    await hass.async_block_till_done()
    assert len(notify_calls) == count  # no repeats

    # own minimum by the entity: 0.8 bar → no longer too low; the dashboard does not overwrite it
    await hass.services.async_call(
        "number",
        "set_value",
        {"entity_id": "number.technik_pressure_min", "value": 0.8},
        blocking=True,
    )
    technik.set_settings({**SETTINGS, "pressure_min": 1.5})
    assert technik.settings()["pressure_min"] == 0.8
    assert "pressure" not in technik.hints


async def test_unreachable(hass: HomeAssistant, hass_ws_client, notify_calls, freezer) -> None:
    entry = await _setup(hass, hass_ws_client)
    hass.states.async_set("sensor.batterie_soc", "unavailable", {})
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert "Batterie: nicht erreichbar" in _messages(notify_calls)
    assert entry.runtime_data.technik.hints


async def test_water_watch_and_valve(
    hass: HomeAssistant, hass_ws_client, notify_calls, freezer
) -> None:
    client = await hass_ws_client(hass)
    freezer.move_to(datetime(2026, 10, 26, 3, 0, tzinfo=dt_util.get_default_time_zone()))
    _states(hass)
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    entry.runtime_data.store.set_notify({"sources": SOURCES})
    await client.send_json({"id": 1, "type": "smarthome_ui/technik/settings", **SETTINGS})
    await client.receive_json()
    close = async_mock_service(hass, "valve", "close_valve")
    hass.states.async_set("sensor.syr_durchfluss", "6.5", {})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=11))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    water = [c for c in notify_calls if "fließt Wasser" in c.data["message"]]
    assert water and "mitten in der Nacht" in water[0].data["message"]
    assert water[0].data["data"]["actions"][0]["action"] == "SH_WASSER_ZU"
    hass.bus.async_fire("mobile_app_notification_action", {"action": "SH_WASSER_ZU"})
    await hass.async_block_till_done()
    assert close[-1].data["entity_id"] == "valve.hauptwasser"


async def test_cycling_and_base_load(
    hass: HomeAssistant, hass_ws_client, notify_calls, freezer
) -> None:
    client = await hass_ws_client(hass)
    freezer.move_to(datetime(2026, 10, 26, 9, 0, tzinfo=dt_util.get_default_time_zone()))
    entry = await _setup(hass, hass_ws_client, client)
    technik = entry.runtime_data.technik
    for _ in range(30):
        hass.states.async_set("binary_sensor.verdichter", "on", {})
        hass.states.async_set("binary_sensor.verdichter", "off", {})
    await hass.async_block_till_done()
    technik.evaluate()
    await hass.async_block_till_done()
    assert any("taktet" in m for m in _messages(notify_calls))

    # base load: usual nights 200 W, last night 600 W
    today = dt_util.now().date()
    technik.data["base_nights"] = {
        (today - timedelta(days=i)).isoformat(): 200.0 for i in range(1, 7)
    }
    technik.data["base_nights"][today.isoformat()] = 600.0
    technik.evaluate()
    await hass.async_block_till_done()
    assert any("Grundlast letzte Nacht 600 W" in m for m in _messages(notify_calls))


async def test_pressure_trend(hass: HomeAssistant, hass_ws_client, notify_calls) -> None:
    entry = await _setup(hass, hass_ws_client)
    technik = entry.runtime_data.technik
    today = dt_util.now().date()
    technik.data["pressure_days"] = {
        (today - timedelta(days=13 - i)).isoformat(): 1.9 - i * 0.02 for i in range(14)
    }
    assert round(technik.pressure_trend(), 2) == -0.14
    technik.evaluate()
    await hass.async_block_till_done()
    assert any("Heizungsdruck fällt (-0,14 bar pro Woche)" in m for m in _messages(notify_calls))
