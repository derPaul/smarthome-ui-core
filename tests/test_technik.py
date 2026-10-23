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


async def test_forecasts(hass: HomeAssistant, hass_ws_client) -> None:
    hass.states.async_set("sensor.bat_soc", "50", {})
    hass.states.async_set("sensor.bat_power", "-500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.ww_ist", "50", {})
    hass.states.async_set("sensor.ww_soll", "50", {})
    entry = await _setup(hass, hass_ws_client)
    technik, fc = entry.runtime_data.technik, entry.runtime_data.forecasts
    technik.set_settings(
        {
            **SETTINGS,
            "battery": {"power": "sensor.bat_power", "soc": "sensor.bat_soc", "capacity": 10},
            "dhw": "sensor.ww_ist",
            "dhw_target": "sensor.ww_soll",
        }
    )
    # battery: not enough data yet, then 3 days of -500 W at night, +2000 W in the day
    assert fc.battery_forecast() == {}
    day = dt_util.now().date()
    fc.data["battery_hours"] = {
        f"{(day - timedelta(days=d)).isoformat()}T{h:02d}": (2000.0 if 10 <= h < 16 else -500.0)
        for d in range(1, 4)
        for h in range(24)
    }
    result = fc.battery_forecast()
    assert result["min"] < 50
    assert result["empty"] or result["charge"]

    # solar window: 4 kWh per hour from 11 to 14 o'clock today
    noon = dt_util.start_of_local_day() + timedelta(hours=11)
    start = int(noon.timestamp() * 1000)
    hours = {start + i * 3_600_000: 4000.0 for i in range(3)}
    window = fc.solar_window(hours, now=start - 3_600_000)
    assert window["start"] == start and window["kwh"] > 9
    assert fc.solar_window({start: 600.0}, now=start - 3_600_000) == {}

    # hot water: heats from 44 to 52, cools down; the start temperature is learned
    t = 1_000_000_000_000
    for i, value in enumerate([46, 45, 44, 47, 50, 52, 51.5, 50, 46, 44.5, 45, 48, 51, 52, 51.4]):
        fc.dhw_sample(value, t + i * 600_000)
    assert fc.data["dhw_starts"] == [44.0, 44.5]
    assert hass.states.get("sensor.technik_dhw_next") is not None


async def test_battery_forecast_with_heat_pump_runs(hass: HomeAssistant, hass_ws_client) -> None:
    hass.states.async_set("sensor.bat_soc", "60", {})
    hass.states.async_set("sensor.bat_power", "-300", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.wp_leistung", "0", {"unit_of_measurement": "W"})
    hass.states.async_set("binary_sensor.heizbetrieb", "off", {})
    entry = await _setup(hass, hass_ws_client)
    technik, fc = entry.runtime_data.technik, entry.runtime_data.forecasts
    technik.set_settings(
        {
            **SETTINGS,
            "battery": {"power": "sensor.bat_power", "soc": "sensor.bat_soc", "capacity": 10},
            "hp_power": "sensor.wp_leistung",
            "heat_mode": "binary_sensor.heizbetrieb",
        }
    )
    day = dt_util.now().date()
    keys = [
        f"{(day - timedelta(days=d)).isoformat()}T{h:02d}" for d in range(1, 4) for h in range(24)
    ]
    fc.data["battery_hours"] = dict.fromkeys(keys, -300.0)
    base = fc.battery_forecast()
    assert base["hp_runs"] == 0  # nothing known about the heat pump yet

    # heat pump: on average 300 W, runs 30 min every 90 min with 1.2 kW
    fc.data["hp_hours"] = dict.fromkeys(keys, 300.0)
    fc.data["hp_run_w"] = 1200.0
    t = 1_000_000_000_000
    for i in range(4):
        fc.heat_changed(True, t + i * 90 * 60_000)
        fc.heat_changed(False, t + i * 90 * 60_000 + 30 * 60_000)
    assert fc.heat_cycle() == (30 * 60_000, 60 * 60_000)
    with_runs = fc.battery_forecast()
    assert with_runs["hp_runs"] > 0
    # 1.2 kW for a third of the time = 400 W instead of the 300 W average: empty sooner
    assert with_runs["empty"] < base["empty"]
