"""Holiday mode: heating down, hot water down, only important notifications, back when home."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


async def test_vacation(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-12-20 10:00:00")
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    runtime.store.set_notify({"sources": {"hh.waste": {"push": True}}})
    temps = async_mock_service(hass, "climate", "set_temperature")
    numbers = async_mock_service(hass, "number", "set_value")
    hass.states.async_set("climate.bad", "heat", {"temperature": 22, "min_temp": 5})
    hass.states.async_set("climate.aus", "off", {"temperature": 20})
    hass.states.async_set("number.lwz_tagbetrieb_temperatursoll_hk_1", "21", {"min": 5, "max": 30})
    hass.states.async_set(
        "number.lwz_komfortbetrieb_warmwasser_temperatursoll", "50", {"min": 10, "max": 65}
    )
    hass.states.async_set("person.anna", "not_home")
    await hass.async_block_till_done()
    vac = runtime.vacation
    until = int((dt_util.utcnow() + timedelta(hours=30)).timestamp() * 1000)
    await vac.async_set(True, {"until": until})
    assert {c.data["entity_id"]: c.data["temperature"] for c in temps} == {"climate.bad": 19.0}
    assert {c.data["entity_id"]: c.data["value"] for c in numbers} == {
        "number.lwz_tagbetrieb_temperatursoll_hk_1": 18.0,
        "number.lwz_komfortbetrieb_warmwasser_temperatursoll": 40.0,
    }
    assert hass.states.get("switch.security_vacation").state == "on"
    # only important notifications: waste is held back, security goes out
    notify_calls.clear()
    assert not runtime.engine.send("Müll", "Restmüll rausstellen", src="hh.waste")
    assert runtime.engine.send("Alarm", "Bewegung im Flur", src="sec.alarm")
    assert vac.info()["suppressed"] == 1
    # 24 h before the return date: heating and hot water back
    temps.clear()
    numbers.clear()
    freezer.tick(timedelta(hours=7))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    assert {c.data["entity_id"]: c.data["temperature"] for c in temps} == {"climate.bad": 22}
    assert vac.info()["preheated"]
    # somebody comes home: off, summary push
    notify_calls.clear()
    hass.states.async_set("person.anna", "home")
    await hass.async_block_till_done()
    assert not vac.on and hass.states.get("switch.security_vacation").state == "off"
    msg = next(c.data for c in notify_calls if c.data.get("title") == "Willkommen zurück")
    assert "1 Mitteilung zurückgehalten" in msg["message"]
    assert vac.settings()["until"] is None
