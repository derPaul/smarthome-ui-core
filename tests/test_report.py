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
    runtime.energy.data["consumers"] = [
        {"entity": "sensor.lwz_warmwasser", "name": "Warmwasser", "power": None},
        {"entity": "sensor.waermepumpe_energie", "name": "Wärmepumpe", "power": None},
    ]
    runtime.energy.data["water"] = ["sensor.wasserzaehler"]
    runtime.report.context = lambda: {"outdoor": {"temperature": "sensor.aussen"}, "technik": {}}
    now = dt_util.utcnow()
    now_ms = int(now.timestamp() * 1000)
    runtime.store.data["data"]["mow_log"] = {"Vorgarten": {"last": now_ms - 3600_000}}
    runtime.store.data["data"]["done_log"] = [
        {"t": now_ms - 7200_000, "k": "x"},
        {"t": now_ms - 9 * 86400_000, "k": "y"},
    ]
    runtime.store.data["doorbells"] = {
        "b": {"rings": [now_ms - 86400_000, now_ms - 2 * 86400_000], "mute": False}
    }
    runtime.store.data["mailbox"]["ins"] = [now_ms - 3 * 86400_000]
    week = {
        "sensor.bezug": 80.0,
        "sensor.einspeisung": 20.0,
        "sensor.pv": 60.0,
        "sensor.waermepumpe_energie": 46.0,
    }
    before = {
        "sensor.bezug": 100.0,
        "sensor.einspeisung": 10.0,
        "sensor.pv": 40.0,
        "sensor.waermepumpe_energie": 50.0,
    }

    def this_week(start) -> bool:
        return start > now - timedelta(days=8)

    async def change(self, sid, start, end):
        return (week if this_week(start) else before).get(sid, 0.0)

    async def volume(self, sid, start, end):
        return 1100.0 if this_week(start) else 1000.0

    async def stat(self, sid, start, end, types, units=None):
        if sid != "sensor.aussen":
            return {}
        return (
            {"mean": 6.1, "min": -2.3, "max": 11.4}
            if this_week(start)
            else {"mean": 4.3, "min": -5.0, "max": 9.0}
        )

    async def daily(self, ids, start, end):
        return [5.0, 9.0, 18.5, 7.0, 6.0, 8.0, 6.5]

    with (
        patch.object(WeeklyReport, "_change", change),
        patch.object(WeeklyReport, "_volume", volume),
        patch.object(WeeklyReport, "_stat", stat),
        patch.object(WeeklyReport, "_daily", daily),
    ):
        report = await runtime.report.async_run(push=False)
        d = report["data"]
        e = d["energy"]
        assert (
            e["used"] == 120.0 and e["paid"] == 24.0 and e["earned"] == 1.6 and e["autarky"] == 33
        )
        assert e["prev"]["used"] == 130.0 and e["best_day"]["kwh"] == 18.5
        assert d["heating"] == {"heatpump": 46.0, "prev": 50.0}
        assert d["water"] == {"liters": 1100.0, "prev": 1000.0}
        assert d["weather"]["mean"] == 6.1 and d["weather"]["prev"]["mean"] == 4.3
        ev = d["events"]
        assert ev["rings"] == 2 and ev["post"] == 1 and ev["done"] == 1 and ev["mowed"] == 1
        assert ev["prev"]["done"] == 1
        lines = report["lines"]
        assert (
            lines[0]
            == "Strom: 120 kWh verbraucht (−8 % zur Vorwoche), 80 kWh aus dem Netz (24,00 €)"
        )
        assert "Solar: 60 kWh (+50 % zur Vorwoche)" in lines[1] and "Autarkie 33 %" in lines[1]
        assert any(x.startswith("Bester Sonnentag: ") and "(18 kWh)" in x for x in lines)
        assert "Heizung: Wärmepumpe 46 kWh Strom (−8 % zur Vorwoche)" in lines
        assert "Wasser: 1.100 L (+10 % zur Vorwoche)" in lines
        assert "Wetter: Ø 6,1°, −2,3° bis 11,4° (1,8 Grad wärmer als die Vorwoche)" in lines
        assert "2× geklingelt · 1× Post · 1 Aufgabe erledigt · 1 Fläche gemäht" in lines
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
