"""Notification rules, reports, quiet hours and due tasks."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util


async def _config(hass: HomeAssistant, entry: MockConfigEntry, **config) -> None:
    entry.runtime_data.store.set_notify(config)
    await hass.async_block_till_done()


async def _tick(hass: HomeAssistant, freezer, seconds: float) -> None:
    freezer.tick(timedelta(seconds=seconds))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()


async def test_state_rule_with_duration(hass: HomeAssistant, entry, notify_calls, freezer) -> None:
    hass.states.async_set("binary_sensor.fenster", "off")
    await _config(
        hass,
        entry,
        rules=[
            {
                "id": "win",
                "type": "state",
                "entity": "binary_sensor.fenster",
                "to": ["on"],
                "for": 600,
                "title": "Fenster",
                "message": "Fenster seit 10 Min. offen",
                "src": "sec.openings",
                "url": "/dashboard/sicherheit",
            }
        ],
    )
    hass.states.async_set("binary_sensor.fenster", "on")
    await hass.async_block_till_done()
    await _tick(hass, freezer, 300)
    assert notify_calls == []
    await _tick(hass, freezer, 330)
    assert len(notify_calls) == 1
    call = notify_calls[0].data
    assert call["message"] == "Fenster seit 10 Min. offen"
    assert call["data"]["url"] == "/dashboard/sicherheit"
    # stays open: no repeat; closed and open again: new notification
    await _tick(hass, freezer, 3600)
    assert len(notify_calls) == 1
    hass.states.async_set("binary_sensor.fenster", "off")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.fenster", "on")
    await hass.async_block_till_done()
    await _tick(hass, freezer, 630)
    assert len(notify_calls) == 2


async def test_no_notification_for_what_is_already_true(
    hass: HomeAssistant, entry, notify_calls
) -> None:
    hass.states.async_set("sensor.waschmaschine", "Fertig")
    await _config(
        hass,
        entry,
        rules=[
            {
                "id": "wm",
                "type": "state",
                "entity": "sensor.waschmaschine",
                "to": ["fertig"],
                "message": "Waschmaschine fertig",
            }
        ],
    )
    assert notify_calls == []
    hass.states.async_set("sensor.waschmaschine", "Läuft")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.waschmaschine", "Fertig")
    await hass.async_block_till_done()
    assert [c.data["message"] for c in notify_calls] == ["Waschmaschine fertig"]


async def test_change_numeric_template_and_urgent(hass: HomeAssistant, entry, notify_calls) -> None:
    hass.states.async_set("event.klingel", "2026-10-21T10:00:00")
    hass.states.async_set("sensor.kuehlschrank", "5")
    hass.states.async_set("sensor.trockner", "aus")
    await _config(
        hass,
        entry,
        rules=[
            {
                "id": "bell",
                "type": "change",
                "entity": "event.klingel",
                "message": "Es hat geklingelt",
                "urgent": True,
            },
            {
                "id": "fridge",
                "type": "numeric",
                "entity": "sensor.kuehlschrank",
                "above": 8,
                "message": "Kühlschrank zu warm",
            },
            {
                "id": "dry",
                "type": "template",
                "template": (
                    "{{ states('sensor.trockner') is search('fertig|ende', ignorecase=True) }}"
                ),
                "message": "Trockner fertig",
            },
        ],
    )
    hass.states.async_set("event.klingel", "2026-10-21T10:05:00")
    hass.states.async_set("sensor.kuehlschrank", "9.5")
    hass.states.async_set("sensor.trockner", "Programm Ende")
    await hass.async_block_till_done()
    messages = {c.data["message"]: c.data for c in notify_calls}
    assert set(messages) == {"Es hat geklingelt", "Kühlschrank zu warm", "Trockner fertig"}
    assert messages["Es hat geklingelt"]["data"]["push"] == {"interruption-level": "time-sensitive"}
    assert "push" not in messages["Kühlschrank zu warm"]["data"]


async def test_reports_are_sent_once(hass: HomeAssistant, entry, notify_calls, freezer) -> None:
    engine = entry.runtime_data.engine
    item = {"key": "mail|1", "title": "Briefkasten", "message": "Post ist da", "src": "hh.mail"}
    engine.report("tablet", [item])
    engine.report("phone", [item])
    await hass.async_block_till_done()
    assert len(notify_calls) == 1
    # gone everywhere for 15 minutes → may come again
    engine.report("tablet", [])
    engine.report("phone", [])
    for _ in range(35):
        await _tick(hass, freezer, 30)
        engine.report("tablet", [])
    engine.report("tablet", [item])
    await hass.async_block_till_done()
    assert len(notify_calls) == 2


async def test_reports_kept_while_no_dashboard_is_open(
    hass: HomeAssistant, entry, notify_calls, freezer
) -> None:
    engine = entry.runtime_data.engine
    item = {"key": "pkg|1", "message": "Paket kommt heute"}
    engine.report("tablet", [item])
    await hass.async_block_till_done()
    await _tick(hass, freezer, 3600)  # tablet off for an hour
    engine.report("tablet", [item])
    await hass.async_block_till_done()
    assert len(notify_calls) == 1


async def test_quiet_hours_and_pause(hass: HomeAssistant, entry, notify_calls, freezer) -> None:
    freezer.move_to(dt_util.as_utc(dt_util.now().replace(hour=23, minute=0)))
    await _config(hass, entry, quiet={"from": "22:00", "to": "07:00"})
    engine = entry.runtime_data.engine
    engine.send("Smarthome", "leise")
    engine.send("Smarthome", "dringend", urgent=True)
    await hass.async_block_till_done()
    assert [c.data["message"] for c in notify_calls] == ["dringend"]
    await _tick(hass, freezer, 9 * 3600)  # 08:00
    assert [c.data["message"] for c in notify_calls] == ["dringend", "leise"]

    await hass.services.async_call(
        "switch",
        "turn_off",
        target={"entity_id": "switch.smarthome_ui_notifications"},
        blocking=True,
    )
    engine.send("Smarthome", "pausiert")
    await hass.async_block_till_done()
    assert len(notify_calls) == 2
    await hass.services.async_call("smarthome_ui", "test_notify", {}, blocking=True)
    await hass.async_block_till_done()
    assert len(notify_calls) == 3, "the test notification ignores the pause"
    assert (
        hass.states.get("sensor.smarthome_ui_last_notification")
        .attributes["recent"][0]["message"]
        .startswith("Test")
    )


async def test_at_rule_and_due_task(hass: HomeAssistant, entry, notify_calls, freezer) -> None:
    freezer.move_to(
        dt_util.as_utc(dt_util.now().replace(year=2026, month=10, day=22, hour=11, minute=59))
    )
    store = entry.runtime_data.store
    at = int(dt_util.as_utc(dt_util.now().replace(hour=12, minute=0, second=0)).timestamp() * 1000)
    store.set_tasks(
        [
            {
                "key": "clean:bad",
                "name": "Bad putzen",
                "src": "hh.tasks",
                "interval": 7,
                "due": at + 3600_000,
            }
        ]
    )
    await _config(
        hass,
        entry,
        rules=[
            {
                "id": "waste1",
                "type": "at",
                "at": at,
                "message": "Morgen: Restmüll",
                "skip_if_done": "waste:2026-10-23",
            },
            {
                "id": "waste2",
                "type": "at",
                "at": at,
                "message": "Morgen: Papier",
                "skip_if_done": "waste:2026-10-24",
            },
        ],
        sources={"hh.tasks": {"push": True}},
        task_hour=9,
    )
    store.mark_done("waste:2026-10-24", by="skip")
    await _tick(hass, freezer, 90)
    assert [c.data["message"] for c in notify_calls] == ["Morgen: Restmüll"]
    await _tick(hass, freezer, 3600)
    assert [c.data["message"] for c in notify_calls][-1] == "Bad putzen"
    await _tick(hass, freezer, 3600)
    assert len(notify_calls) == 2


async def test_calendar_rule(hass: HomeAssistant, entry, notify_calls, freezer) -> None:
    from pytest_homeassistant_custom_component.common import async_mock_service

    from homeassistant.core import SupportsResponse

    freezer.move_to(
        dt_util.as_utc(dt_util.now().replace(year=2026, month=10, day=21, hour=7, minute=55))
    )
    start = dt_util.now().replace(hour=9, minute=0, second=0, microsecond=0)
    async_mock_service(
        hass,
        "calendar",
        "get_events",
        response={
            "calendar.familie": {
                "events": [
                    {"start": start.isoformat(), "end": start.isoformat(), "summary": "Zahnarzt"},
                    {"start": "2026-10-22", "end": "2026-10-23", "summary": "Geburtstag Oma"},
                ]
            }
        },
        supports_response=SupportsResponse.ONLY,
    )
    await _config(
        hass,
        entry,
        rules=[
            {
                "id": "cal",
                "type": "calendar",
                "entities": ["calendar.familie"],
                "lead": 30,
                "all_day_days": 1,
                "all_day_at": "08:00",
                "src": "cal.events",
            }
        ],
    )
    await hass.async_block_till_done()
    await _tick(hass, freezer, 6 * 60)  # 08:01
    assert [c.data["message"] for c in notify_calls] == ["Morgen: Geburtstag Oma"]
    await _tick(hass, freezer, 30 * 60)  # 08:31
    assert [c.data["message"] for c in notify_calls][-1] == "09:00 · Zahnarzt"
    await _tick(hass, freezer, 30 * 60)
    assert len(notify_calls) == 2
