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
    meters = {"heat_out": "sensor.heizwaerme", "dhw_out": "sensor.ww_waerme", "heat_in": None}
    runtime.report.context = lambda: {
        "outdoor": {"temperature": "sensor.aussen"},
        "technik": {"hp_meters": meters},
    }
    # two earlier weeks with similar weather and a better coefficient of performance
    runtime.store.data["hp_weeks"] = [
        {"t": 1, "cop": 4.0, "out": 5.5},
        {"t": 2, "cop": 3.9, "out": 7.0},
        {"t": 3, "cop": 2.5, "out": -3.0},
    ]
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
        "sensor.heizwaerme": 130.0,
        "sensor.ww_waerme": 20.0,
    }
    before = {
        "sensor.bezug": 100.0,
        "sensor.einspeisung": 10.0,
        "sensor.pv": 40.0,
        "sensor.waermepumpe_energie": 50.0,
        "sensor.heizwaerme": 160.0,
        "sensor.ww_waerme": 20.0,
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
        h = d["heating"]
        assert h["heatpump"] == 46.0 and h["prev"] == 50.0 and h["heat"] == 150.0
        assert h["cop"] == 3.26 and h["cop_prev"] == 3.6 and h["outdoor"] == 6.1
        assert h["warn"] == {"usual": 3.95, "weeks": 2} and h["history"][-1]["cop"] == 3.26
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
        assert (
            "Heizung: Wärmepumpe 46 kWh Strom (−8 % zur Vorwoche), 150 kWh Wärme, "
            "Arbeitszahl 3,3 (Vorwoche 3,6)"
        ) in lines
        assert any(x.startswith("Achtung Wärmepumpe: Arbeitszahl 3,3") for x in lines)
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
        # the scheduled run keeps the heat pump's week
        assert len(runtime.store.data["hp_weeks"]) == 4
        assert runtime.store.data["hp_weeks"][-1]["cop"] == 3.26
        # Monday: nothing
        notify_calls.clear()
        nxt = local + timedelta(days=1)
        freezer.move_to(nxt)
        async_fire_time_changed(hass, nxt)
        await hass.async_block_till_done()
        assert not notify_calls

        # 1st of the month 09:12: balance of the month before, with the month and year before
        first = dt_util.as_local(dt_util.now()).replace(
            month=12, day=1, hour=9, minute=12, second=0
        )
        freezer.move_to(first)
        async_fire_time_changed(hass, first)
        await hass.async_block_till_done()
        msg = next(c.data for c in notify_calls if c.data.get("title", "").startswith("Monats"))
        assert msg["title"] == "Monatsbilanz November 2026"
        rm = runtime.report.info_month()
        assert rm["month"] and rm["data"]["energy"]["year"] is not None
        assert "(gleich wie Vormonat, gleich wie Vorjahr)" in rm["lines"][0]
