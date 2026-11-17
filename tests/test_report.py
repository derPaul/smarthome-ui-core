"""Weekly report: energy with prices of the energy dashboard, robots, tasks; push on Sunday."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.smarthome_ui.energy import energy_from_prefs
from custom_components.smarthome_ui.report import WeeklyReport
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


def test_prices_from_energy_prefs() -> None:
    out = energy_from_prefs(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.bezug",
                    "stat_energy_to": "sensor.einspeisung",
                    "number_energy_price": 0.2986,
                    "number_energy_price_export": 0.0778,
                }
            ]
        }
    )
    assert out["price_import"] == 0.2986 and out["price_export"] == 0.0778
    old = energy_from_prefs(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "flow_from": [
                        {"stat_energy_from": "sensor.a", "entity_energy_price": "sensor.preis"}
                    ],
                    "flow_to": [{"stat_energy_to": "sensor.b", "number_energy_price": 0.08}],
                }
            ]
        }
    )
    assert old["price_import_entity"] == "sensor.preis" and old["price_export"] == 0.08


async def test_weekly_report(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-11-15 10:00:00")  # Sunday (test time zone US/Pacific: 02:00)
    entry = MockConfigEntry(domain="smarthome_ui", data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    runtime = entry.runtime_data
    runtime.energy.data = energy_from_prefs(
        {
            "energy_sources": [
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.bezug",
                    "stat_energy_to": "sensor.einspeisung",
                    "number_energy_price": 0.30,
                    "number_energy_price_export": 0.08,
                },
                {"type": "solar", "stat_energy_from": "sensor.pv"},
            ]
        }
    )
    now = dt_util.utcnow()
    runtime.store.data["data"]["mow_log"] = {
        "Vorgarten": {"last": int(now.timestamp() * 1000) - 3600_000}
    }
    values = {"sensor.bezug": 80.0, "sensor.einspeisung": 20.0, "sensor.pv": 60.0}

    async def change(self, sid, start, end):
        return values.get(sid, 0.0)

    with patch.object(WeeklyReport, "_change", change):
        report = await runtime.report.async_run(push=False)
        e = report["data"]["energy"]
        assert (
            e["used"] == 120.0 and e["paid"] == 24.0 and e["earned"] == 1.6 and e["autarky"] == 33
        )
        assert report["lines"][0].startswith(
            "Strom: 120 kWh verbraucht, 80 kWh aus dem Netz (24,00 €)"
        )
        assert "Autarkie 33 %" in report["lines"][1]
        assert "1 Fläche gemäht" in report["lines"]
        assert not notify_calls

        # Sunday 18:12 local: push
        runtime.store.set_notify({"sources": {"home.report": {"push": True}}})
        local = dt_util.now().replace(hour=18, minute=12, second=0)
        freezer.move_to(local)
        async_fire_time_changed(hass, local)
        await hass.async_block_till_done()
        msg = next(c.data for c in notify_calls if c.data.get("title") == "Wochenbericht")
        assert "Strom: 120 kWh" in msg["message"]
        # Monday: nothing
        notify_calls.clear()
        nxt = local + timedelta(days=1)
        freezer.move_to(nxt)
        async_fire_time_changed(hass, nxt)
        await hass.async_block_till_done()
        assert not notify_calls
