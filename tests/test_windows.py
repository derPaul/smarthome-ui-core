"""Windows as devices: found by themselves, heat protection, lockout, winter, cleaning."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_mock_service,
)

from custom_components.smarthome_ui.const import DOMAIN
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import area_registry as ar, entity_registry as er


async def _setup(hass: HomeAssistant) -> MockConfigEntry:
    areas = ar.async_get(hass)
    ents = er.async_get(hass)
    office = areas.async_create("Arbeitszimmer")
    living = areas.async_create("Wohnzimmer")
    for object_id, area in (
        ("arbeitszimmer_rollladen", office),
        ("terrassentur", living),
        ("garage", living),
    ):
        entry = ents.async_get_or_create("cover", "demo", object_id, suggested_object_id=object_id)
        ents.async_update_entity(entry.entity_id, area_id=area.id)
    contact = ents.async_get_or_create(
        "binary_sensor", "demo", "az_fenster", suggested_object_id="az_fenster"
    )
    ents.async_update_entity(contact.entity_id, area_id=office.id)
    attrs = {"device_class": "shutter", "current_position": 100, "supported_features": 15}
    hass.states.async_set("cover.arbeitszimmer_rollladen", "open", {**attrs, "friendly_name": "AZ"})
    hass.states.async_set("cover.terrassentur", "open", {**attrs, "friendly_name": "Terrassentür"})
    hass.states.async_set("cover.garage", "closed", {"device_class": "garage"})
    hass.states.async_set("binary_sensor.az_fenster", "off", {"device_class": "window"})
    hass.states.async_set("sun.sun", "above_horizon", {"azimuth": 100, "elevation": 30})
    hass.states.async_set("weather.home", "sunny", {"temperature": 28})
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_windows(hass: HomeAssistant, notify_calls, hass_ws_client) -> None:
    calls = {
        s: async_mock_service(hass, "cover", s)
        for s in ("open_cover", "close_cover", "set_cover_position")
    }
    entry = await _setup(hass)
    win = entry.runtime_data.windows
    assert not win.settings()["heat_on"]  # nothing moves until chosen
    win.set_settings({"heat_on": True, "morning_on": True, "evening_on": True})
    # every shutter became a window (not the garage door); contact guessed in its area
    assert sorted(win.items) == ["arbeitszimmer", "wohnzimmer"]
    office = win.items["arbeitszimmer"]
    assert office["name"] == "Fenster Arbeitszimmer"
    assert office["contact"] == "binary_sensor.az_fenster"
    assert win.items["wohnzimmer"]["name"] == "Fenster Wohnzimmer"
    # a device per window with its entities
    assert hass.states.get("select.window_arbeitszimmer_orientation").state == "unknown"
    assert hass.states.get("switch.window_arbeitszimmer_automatic").state == "on"
    # heat protection: needs the orientation, then down while the sun is on the facade
    await win.async_heat()
    assert calls["set_cover_position"] == []
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.window_arbeitszimmer_orientation", "option": "e"},
        blocking=True,
    )
    assert office["orientation"] == "e"
    await win.async_heat()
    assert [c.data["entity_id"] for c in calls["set_cover_position"]] == [
        "cover.arbeitszimmer_rollladen"
    ]
    assert calls["set_cover_position"][0].data["position"] == 25
    assert office["heat"] == {"from": 100, "to": 25.0}
    # the sun went round: back up (nobody moved it meanwhile)
    hass.states.async_set(
        "cover.arbeitszimmer_rollladen",
        "open",
        {"device_class": "shutter", "current_position": 25},
        context=Context(id=next(iter(win._ctx))),
    )
    await hass.async_block_till_done()
    assert "manual" not in office
    hass.states.async_set("sun.sun", "above_horizon", {"azimuth": 250, "elevation": 30})
    await win.async_heat()
    assert [c.data["entity_id"] for c in calls["open_cover"]] == ["cover.arbeitszimmer_rollladen"]
    assert "heat" not in office
    # moved by hand: the heat protection leaves it alone
    hass.states.async_set(
        "cover.arbeitszimmer_rollladen", "open", {"device_class": "shutter", "current_position": 60}
    )
    await hass.async_block_till_done()
    assert office.get("manual")
    hass.states.async_set("sun.sun", "above_horizon", {"azimuth": 90, "elevation": 30})
    await win.async_heat()
    assert len(calls["set_cover_position"]) == 1
    # lockout: the open terrace door is not closed in the evening, it follows once shut
    hass.states.async_set("binary_sensor.terrasse", "on", {"device_class": "door"})
    win.set_window("wohnzimmer", {"door": True, "contact": "binary_sensor.terrasse"})
    await win.async_evening()
    assert [c.data["entity_id"] for c in calls["close_cover"]] == ["cover.arbeitszimmer_rollladen"]
    assert win.items["wohnzimmer"]["pending"] == "evening"
    hass.states.async_set("binary_sensor.terrasse", "off", {"device_class": "door"})
    await hass.async_block_till_done()
    assert calls["close_cover"][-1].data["entity_id"] == "cover.terrassentur"
    # winter: no heat protection
    hass.states.async_set("weather.home", "sunny", {"temperature": 3})
    assert win.winter()
    office.pop("manual")
    await win.async_heat()
    assert len(calls["set_cover_position"]) == 1
    # cleaning: never cleaned is due; the button marks it
    assert win.clean_due(office)
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.window_arbeitszimmer_clean"}, blocking=True
    )
    assert not win.clean_due(office)
    assert hass.states.get("sensor.window_arbeitszimmer_cleaned").state not in ("unknown", "")
    # dashboard: info, add a window without cover, settings, remove (does not come back)
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "smarthome_ui/windows/set", "name": "Fenster Bad", "area_id": None}
    )
    res = (await client.receive_json())["result"]
    assert {i["id"] for i in res["items"]} == {"arbeitszimmer", "wohnzimmer", "bad"}
    await client.send_json_auto_id(
        {"type": "smarthome_ui/windows/settings", "heat_temp": 26, "winter": "off"}
    )
    res = (await client.receive_json())["result"]
    assert res["settings"]["heat_temp"] == 26 and res["winter"] is False
    await client.send_json_auto_id(
        {"type": "smarthome_ui/windows/remove", "window_id": "wohnzimmer"}
    )
    await client.receive_json()
    win.discover()
    assert "wohnzimmer" not in win.items
    await hass.async_block_till_done()
    assert hass.states.get("switch.window_bad_automatic").state == "on"


async def test_window_cleaning_per_room(hass: HomeAssistant, notify_calls, hass_ws_client) -> None:
    entry = await _setup(hass)
    win = entry.runtime_data.windows
    win.set_window(
        None, {"name": "Fenster Arbeitszimmer 2", "area_id": win.items["arbeitszimmer"]["area_id"]}
    )
    tasks = {t["area"]: t for t in win.tasks()}
    assert tasks["Arbeitszimmer"]["name"] == "Fenster putzen (2)"
    assert tasks["Arbeitszimmer"]["last"] is None
    # one window cleaned: the room stays due (the other one is not)
    win.cleaned(["arbeitszimmer"])
    assert {t["area"]: t for t in win.tasks()}["Arbeitszimmer"]["last"] is None
    # the room's task checked off: every window of the room counts as cleaned
    client = await hass_ws_client(hass)
    key = tasks["Arbeitszimmer"]["key"]
    await client.send_json_auto_id(
        {"type": "smarthome_ui/done", "changes": {key: {"t": 1_900_000_000_000, "by": "x"}}}
    )
    assert (await client.receive_json())["success"]
    assert not any(
        win.clean_due(w)
        for w in win.items.values()
        if w.get("area_id") == win.items["arbeitszimmer"]["area_id"]
    )
    assert win.clean_due(win.items["wohnzimmer"])
    assert any(t["key"] == key for t in entry.runtime_data.tasks())


async def test_windows_by_brightness(hass: HomeAssistant, notify_calls, freezer) -> None:
    """Closed when the camera says dark for a while, opened when light – not before the morning."""
    from datetime import timedelta  # noqa: PLC0415

    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        async_fire_time_changed,
    )

    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    calls = {s: async_mock_service(hass, "cover", s) for s in ("open_cover", "close_cover")}
    hass.states.async_set("binary_sensor.g6_entry_ist_dunkel", "off")
    freezer.move_to(dt_util.as_utc(dt_util.now().replace(hour=16, minute=30)))
    entry = await _setup(hass)
    win = entry.runtime_data.windows
    assert win.dark_sensor() == "binary_sensor.g6_entry_ist_dunkel"  # found by itself
    win.set_settings(
        {
            "trigger": "dark",
            "morning_on": True,
            "evening_on": True,
            "dark_delay": 10,
            "stagger": 0,
            "morning": "07:00",
            "morning_weekend": "07:00",
        }
    )

    async def later(minutes: float) -> None:
        freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(hass)
        await hass.async_block_till_done()

    # flickers for 3 minutes: nothing
    hass.states.async_set("binary_sensor.g6_entry_ist_dunkel", "on")
    await later(3)
    hass.states.async_set("binary_sensor.g6_entry_ist_dunkel", "off")
    await later(11)
    assert calls["close_cover"] == []
    # dark for 10 minutes: every window closes, one after another in the order of the rooms
    hass.states.async_set("binary_sensor.g6_entry_ist_dunkel", "on")
    await later(11)
    assert [c.data["entity_id"] for c in calls["close_cover"]] == [
        "cover.arbeitszimmer_rollladen",
        "cover.terrassentur",
    ]
    # a restart (unavailable and back) is no new dusk
    hass.states.async_set("binary_sensor.g6_entry_ist_dunkel", "unavailable")
    await later(1)
    hass.states.async_set("binary_sensor.g6_entry_ist_dunkel", "on")
    await later(11)
    assert len(calls["close_cover"]) == 2
    # light at 06:20: waits for the morning time
    freezer.move_to(
        dt_util.as_utc((dt_util.now() + timedelta(days=1)).replace(hour=6, minute=20, second=0))
    )
    hass.states.async_set("binary_sensor.g6_entry_ist_dunkel", "off")
    await later(11)
    assert calls["open_cover"] == []
    await later(30)  # 07:01
    assert len(calls["open_cover"]) == 2
    assert win.info()["last_run"]["why"] == "morning"
    # staggered: one by one with a pause in between (give or take a half)
    win.set_settings({"stagger": 30})
    with patch("custom_components.smarthome_ui.windows.asyncio.sleep", AsyncMock()) as sleep:
        await win._sequence(None, "evening", stagger=True)
    assert len(calls["close_cover"]) == 4
    assert sleep.await_count == 1 and 15 <= sleep.await_args.args[0] <= 45


async def test_window_follows_rename(hass: HomeAssistant, notify_calls) -> None:
    """A renamed cover stays the same window; a deleted one takes its window device along."""
    from homeassistant.helpers import device_registry as dr  # noqa: PLC0415

    entry = await _setup(hass)
    win = entry.runtime_data.windows
    ents = er.async_get(hass)
    win.set_window("arbeitszimmer", {"orientation": "e"})
    ents.async_update_entity("cover.arbeitszimmer_rollladen", new_entity_id="cover.buero_rollladen")
    await hass.async_block_till_done()
    assert win.items["arbeitszimmer"]["cover"] == "cover.buero_rollladen"
    assert win.items["arbeitszimmer"]["orientation"] == "e"
    # renamed while Core was not listening: the stale window goes, the cover comes back as one
    win.items["arbeitszimmer"]["cover"] = "cover.eve_shutter_switch_x"
    hass.states.async_set(
        "cover.buero_rollladen", "open", {"device_class": "shutter", "friendly_name": "Büro"}
    )
    win._discover_soon()
    await hass.async_block_till_done()
    assert [w["cover"] for w in win.items.values()].count("cover.buero_rollladen") == 1
    assert "cover.eve_shutter_switch_x" not in [w["cover"] for w in win.items.values()]
    # removed in the dashboard: its device goes as well
    devices = dr.async_get(hass)
    wid = next(k for k, w in win.items.items() if w["cover"] == "cover.terrassentur")
    dev_id = ents.async_get(f"switch.window_{wid}_automatic").device_id
    assert devices.async_get(dev_id) is not None
    win.remove(wid)
    await hass.async_block_till_done()
    assert devices.async_get(dev_id) is None
    assert hass.states.get(f"switch.window_{wid}_automatic") is None


def test_windows_blueprint_valid() -> None:
    from pathlib import Path  # noqa: PLC0415

    from homeassistant.components.blueprint import models  # noqa: PLC0415
    from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA  # noqa: PLC0415
    from homeassistant.util.yaml import load_yaml  # noqa: PLC0415

    path = Path(__file__).parent.parent / "custom_components/smarthome_ui/blueprints"
    bp = models.Blueprint(
        load_yaml(path / "windows_brightness.yaml"),
        expected_domain="automation",
        schema=BLUEPRINT_SCHEMA,
    )
    assert bp.metadata["source_url"].endswith("windows_brightness.yaml")


async def test_windows_automation(hass: HomeAssistant, notify_calls, freezer) -> None:
    """The blueprint as a real automation: dark → all close, light → open, not before morning."""
    from datetime import datetime, timedelta  # noqa: PLC0415

    from pytest_homeassistant_custom_component.common import (  # noqa: PLC0415
        async_fire_time_changed,
    )

    from homeassistant.setup import async_setup_component  # noqa: PLC0415
    from homeassistant.util import dt as dt_util  # noqa: PLC0415

    tz = dt_util.get_default_time_zone()
    freezer.move_to(dt_util.as_utc(datetime(2026, 12, 7, 16, 30, tzinfo=tz)))  # Monday
    calls = {s: async_mock_service(hass, "cover", s) for s in ("open_cover", "close_cover")}
    hass.states.async_set("binary_sensor.cam_ist_dunkel", "off")
    entry = await _setup(hass)
    assert await async_setup_component(
        hass,
        "automation",
        {
            "automation": {
                "id": "rollos",
                "alias": "Rollläden nach Helligkeit",
                "use_blueprint": {
                    "path": "smarthome_ui/windows_brightness.yaml",
                    "input": {"dark_sensor": "binary_sensor.cam_ist_dunkel", "stagger": 0},
                },
            }
        },
    )
    await hass.async_block_till_done()

    async def later(minutes: float) -> None:
        freezer.tick(timedelta(minutes=minutes))
        async_fire_time_changed(hass)
        await hass.async_block_till_done(wait_background_tasks=True)

    # dark for 10 minutes (HA's "for": flicker does not count): all windows close
    hass.states.async_set("binary_sensor.cam_ist_dunkel", "on")
    await hass.async_block_till_done()
    await later(11)
    assert [c.data["entity_id"] for c in calls["close_cover"]] == [
        "cover.arbeitszimmer_rollladen",
        "cover.terrassentur",
    ]
    # restart: unavailable → on is no dusk
    hass.states.async_set("binary_sensor.cam_ist_dunkel", "unavailable")
    await later(1)
    hass.states.async_set("binary_sensor.cam_ist_dunkel", "on")
    await later(11)
    assert len(calls["close_cover"]) == 2
    # light at 06:30 the next morning: waits for 07:00, then opens (once)
    freezer.move_to(dt_util.as_utc(datetime(2026, 12, 8, 6, 30, tzinfo=tz)))
    hass.states.async_set("binary_sensor.cam_ist_dunkel", "off")
    await later(11)
    assert calls["open_cover"] == []
    await later(20)  # 07:01
    assert len(calls["open_cover"]) == 2
    # the service on its own: not twice the same; with once off it moves again
    win = entry.runtime_data.windows
    res = await hass.services.async_call(
        "smarthome_ui", "windows_move", {"action": "open"}, blocking=True, return_response=True
    )
    assert res == {"started": 0, "skipped": True}
    await hass.services.async_call(
        "smarthome_ui",
        "windows_move",
        {"action": "open", "once": False, "stagger": 0},
        blocking=True,
    )
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(calls["open_cover"]) == 4
    assert win.info()["last_run"]["why"] == "morning"
