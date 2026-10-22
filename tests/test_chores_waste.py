"""Tasks computed from definitions; waste collection from calendars."""

from __future__ import annotations

from datetime import datetime, timedelta

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
)

from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.util import dt as dt_util


def _ms(text: str) -> int:
    return int(
        datetime.fromisoformat(text).replace(tzinfo=dt_util.get_default_time_zone()).timestamp()
        * 1000
    )


async def test_tasks_from_definitions(hass: HomeAssistant, entry: MockConfigEntry, freezer) -> None:
    freezer.move_to(datetime(2026, 3, 15, 10, tzinfo=dt_util.get_default_time_zone()))
    store = entry.runtime_data.store
    store.apply_done(
        {
            "hh:task:filter|": {"t": _ms("2026-01-10 18:00"), "by": "hand"},
            "clean:bad": {"t": _ms("2026-03-12 09:00"), "by": "hand"},
        }
    )
    store.set_data({"household": {"cleaned": {"kueche": {"r": _ms("2026-03-14 11:00")}}}})
    store.set_task_defs(
        [
            {
                "scope": "hh",
                "id": "filter",
                "name": "Filter reinigen",
                "every": {"n": 3, "unit": "m"},
                "areas": [],
            },
            {
                "scope": "gd",
                "id": "duengen",
                "name": "Rasen düngen",
                "windows": [[3, 4]],
                "areas": [None],
            },
            {
                "scope": "hh",
                "id": "rauch",
                "name": "Rauchmelder testen",
                "every": {"n": 1, "unit": "w"},
                "areas": ["bad"],
                "names": {"bad": "Bad"},
            },
        ],
        [
            {"area": "kueche", "name": "Küche", "interval": 2},
            {"area": "bad", "name": "Bad", "interval": 4},
            {"area": "keller", "name": "Keller", "interval": 0},
        ],
    )
    await hass.async_block_till_done()
    tasks = {t["key"]: t for t in store.data["tasks"]}
    assert tasks["hh:task:filter|"]["due"] == _ms("2026-04-10 00:00")
    assert tasks["gd:task:duengen|"]["due"] == _ms("2026-03-01 00:00")  # in the window, not done
    assert tasks["hh:task:rauch|bad"]["name"] == "Rauchmelder testen · Bad"
    assert tasks["hh:task:rauch|bad"]["due"] is None  # never done
    assert tasks["clean:kueche"]["due"] == _ms("2026-03-16 11:00")
    assert tasks["clean:bad"]["due"] == _ms("2026-03-16 09:00")
    assert "clean:keller" not in tasks

    # the dashboard's old sync is ignored now
    store.set_tasks([])  # (what tasks/sync would do without definitions)
    store.recompute_tasks()
    assert len(store.data["tasks"]) == 5

    # garden calendar done → next window next year
    store.apply_done({"gd:task:duengen|": {"t": _ms("2026-03-15 10:00"), "by": "hand"}})
    await hass.async_block_till_done()
    tasks = {t["key"]: t for t in store.data["tasks"]}
    assert tasks["gd:task:duengen|"]["due"] == _ms("2027-03-01 00:00")
    assert hass.states.get("sensor.garden_rasen_dungen") is not None


async def test_waste(
    hass: HomeAssistant, entry: MockConfigEntry, notify_calls, freezer, hass_ws_client
) -> None:
    freezer.move_to(datetime(2026, 3, 2, 8, tzinfo=dt_util.get_default_time_zone()))
    hass.states.async_set("calendar.abfallkalender", "off", {"friendly_name": "Abfallkalender"})

    async def get_events(call: ServiceCall) -> ServiceResponse:
        return {
            "calendar.abfallkalender": {
                "events": [
                    {"summary": "Restabfall", "start": "2026-03-03", "end": "2026-03-04"},
                    {"summary": "Papier, Pappe", "start": "2026-03-03", "end": "2026-03-04"},
                    {"summary": "Bioabfall", "start": "2026-03-06", "end": "2026-03-07"},
                ]
            }
        }

    hass.services.async_register(
        "calendar", "get_events", get_events, supports_response=SupportsResponse.ONLY
    )
    runtime = entry.runtime_data
    runtime.store.set_notify({"sources": {"hh.waste": {"push": True}}})
    # (what the dashboard sends with waste/settings)
    runtime.waste.set_settings({"calendars": [], "remind": {"d": 1, "t": "12:00"}})
    await hass.async_block_till_done()

    nxt = hass.states.get("sensor.household_waste_next")
    assert nxt.state == "2026-03-03"
    assert nxt.attributes["kinds"] == ["Restmüll", "Papier"]
    assert hass.states.get("sensor.household_waste_biotonne").state == "2026-03-06"
    assert runtime.waste.tasks() == []

    # 12:00 the day before: reminder, to-do item and push
    freezer.move_to(datetime(2026, 3, 2, 12, 1, tzinfo=dt_util.get_default_time_zone()))
    # (the integration was set up before the clock was moved back – run the tick directly)
    runtime.engine._tick(dt_util.utcnow())
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Morgen: Restmüll, Papier rausstellen"
    items = await hass.services.async_call(
        "todo",
        "get_items",
        {},
        target={"entity_id": "todo.household"},
        blocking=True,
        return_response=True,
    )
    assert [i["summary"] for i in items["todo.household"]["items"]] == [
        "Morgen: Restmüll, Papier rausstellen"
    ]

    # put out → done, gone from the list
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.household_waste_put_out"}, blocking=True
    )
    await hass.async_block_till_done()
    assert runtime.store.done["waste:2026-03-03"]["t"] > 0
    assert runtime.waste.tasks() == []
    assert hass.states.get("sensor.household_waste_next").attributes["put_out"] is True
    freezer.tick(timedelta(hours=1))
