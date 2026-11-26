"""Windows as devices: found by themselves, heat protection, lockout, winter, cleaning."""

from __future__ import annotations

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
