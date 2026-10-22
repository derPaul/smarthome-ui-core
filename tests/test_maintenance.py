"""Maintenance parts: subentry, own device, due dates, buttons, moving dashboard tasks."""

from __future__ import annotations

from datetime import timedelta
import time

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.smarthome_ui.const import DOMAIN
from custom_components.smarthome_ui.maintenance import detect_kind
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr


def test_detect_kind() -> None:
    assert detect_kind("Kohlefilter tauschen") == "carbon_filter"
    assert detect_kind("Aktivkohlefilter Dunstabzug") == "carbon_filter"
    assert detect_kind("Fettfilter Dunstabzug reinigen") == "hood_filter"
    assert detect_kind("Rückspülfilter rückspülen") == "backwash_filter"
    assert detect_kind("Rauchmelder testen") == "smoke_detector"
    assert detect_kind("Fenster putzen") is None


async def test_add_backwash_filter(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    keller = ar.async_get(hass).async_get_or_create("Keller")
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "part"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"kind": "backwash_filter", "area_id": keller.id}
    )
    assert result["step_id"] == "intervals"
    schema = result["data_schema"]({})
    assert schema == {"clean_every": 6, "replace_every": 0}
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"clean_every": 2, "replace_every": 0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Rückspülfilter Keller"
    await hass.async_block_till_done()

    state = hass.states.get("sensor.household_ruckspulfilter_keller_next_clean")
    assert state is not None
    assert state.attributes["interval_months"] == 2
    assert hass.states.get("sensor.household_ruckspulfilter_keller_next_replace") is None
    assert hass.states.get("button.household_ruckspulfilter_keller_cleaned") is not None
    device = next(
        d for d in dr.async_get(hass).devices.values() if d.name == "Rückspülfilter Keller"
    )
    assert device.area_id == keller.id
    assert device.model == "Rückspülfilter"


async def test_due_and_buttons(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-01-10 08:00:00")
    bad = ar.async_get(hass).async_get_or_create("Bad")
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Smarthome UI Core",
        subentries_data=[
            ConfigSubentryData(
                data={
                    "kind": "carbon_filter",
                    "area_id": bad.id,
                    "since": int(time.time() * 1000),
                    "clean_every": 3,
                    "replace_every": 12,
                },
                subentry_type="part",
                title="Kohlefilter Bad",
                unique_id=None,
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_kohlefilter_bad_next_clean").state == "2026-04-10"
    assert hass.states.get("sensor.household_kohlefilter_bad_next_replace").state == "2027-01-10"
    assert hass.states.get("todo.household").state == "0"

    freezer.move_to("2026-04-11 10:00:00")
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    todo = hass.states.get("todo.household")
    assert todo.state == "1"
    assert hass.states.get("sensor.household_kohlefilter_bad_next_clean").attributes["due"]

    # replaced: counts as cleaned too
    await hass.services.async_call(
        "button", "press", {"entity_id": "button.household_kohlefilter_bad_replaced"}, blocking=True
    )
    await hass.async_block_till_done()
    assert hass.states.get("sensor.household_kohlefilter_bad_next_clean").state == "2026-07-11"
    assert hass.states.get("sensor.household_kohlefilter_bad_next_replace").state == "2027-04-11"
    assert hass.states.get("todo.household").state == "0"
    info = entry.runtime_data.parts.info()[0]
    assert info["actions"]["clean"]["verb"] == "reinigen"
    assert info["device_id"]

    # due again: check it off in the to-do list
    freezer.tick(timedelta(days=95))
    async_fire_time_changed(hass)
    await hass.async_block_till_done()
    await hass.services.async_call(
        "todo",
        "update_item",
        {"entity_id": "todo.household", "item": "Kohlefilter Bad reinigen", "status": "completed"},
        blocking=True,
    )
    await hass.async_block_till_done()
    assert hass.states.get("todo.household").state == "0"


async def test_moves_dashboard_tasks(hass: HomeAssistant, hass_storage, freezer) -> None:
    freezer.move_to("2026-05-01 08:00:00")
    areas = ar.async_get(hass)
    bad = areas.async_get_or_create("Bad")
    wc = areas.async_get_or_create("Gäste-WC")
    wohn = areas.async_get_or_create("Wohnzimmer")
    cleaned_at = int(time.time() * 1000) - 30 * 86_400_000
    hass_storage["smarthome_ui_core"] = {
        "version": 1,
        "key": "smarthome_ui_core",
        "data": {
            "data": {
                "done": {f"hh:task:kf_clean|{bad.id}": {"t": cleaned_at, "by": "hand"}},
                "done_log": [],
                "household": {},
                "mow_log": {},
            },
            "task_defs": {
                "configured": True,
                "rooms": [],
                "tasks": [
                    {
                        "scope": "hh",
                        "id": "kf_clean",
                        "name": "Kohlefilter reinigen",
                        "every": {"n": 3, "unit": "m"},
                        "areas": [bad.id, wc.id],
                        "names": {bad.id: "Bad", wc.id: "Gäste-WC"},
                    },
                    {
                        "scope": "hh",
                        "id": "kf_swap",
                        "name": "Kohlefilter tauschen",
                        "every": {"n": 12, "unit": "m"},
                        "areas": [bad.id],
                        "names": {bad.id: "Bad"},
                    },
                    {
                        "scope": "hh",
                        "id": "rauch",
                        "name": "Rauchmelder testen",
                        "every": {"n": 12, "unit": "m"},
                        "areas": [],
                    },
                    {
                        "scope": "hh",
                        "id": "gitter",
                        "name": "Filtergitter reinigen",
                        "every": {"n": 2, "unit": "m"},
                        "areas": [wohn.id],
                    },
                    {
                        "scope": "hh",
                        "id": "kf_wohn",
                        "name": "Kohlefilter tauschen",
                        "every": {"n": 12, "unit": "m"},
                        "areas": [wohn.id],
                    },
                    {
                        "scope": "hh",
                        "id": "fenster",
                        "name": "Fenster putzen",
                        "every": {"n": 2, "unit": "m"},
                        "areas": [],
                    },
                ],
            },
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    parts = {s.title: dict(s.data) for s in entry.subentries.values() if s.subentry_type == "part"}
    assert set(parts) == {
        "Kohlefilter Bad",
        "Kohlefilter Gäste-WC",
        "Kohlefilter Wohnzimmer",
        "Rauchmelder",
    }
    assert parts["Kohlefilter Wohnzimmer"]["clean_every"] == 2
    assert parts["Kohlefilter Wohnzimmer"]["replace_every"] == 12
    assert parts["Kohlefilter Bad"]["clean_every"] == 3
    assert parts["Kohlefilter Bad"]["replace_every"] == 12
    assert parts["Kohlefilter Gäste-WC"]["replace_every"] == 0
    assert parts["Rauchmelder"]["clean_every"] == 12
    store = entry.runtime_data.store
    assert sorted(store.data["parts_migrated"]) == [
        "gitter",
        "kf_clean",
        "kf_swap",
        "kf_wohn",
        "rauch",
    ]
    # only "Fenster putzen" stays a dashboard task
    assert [t["name"] for t in store.data["tasks"]] == ["Fenster putzen"]
    bad_part = next(
        p for p in entry.runtime_data.parts.parts.values() if p.name == "Kohlefilter Bad"
    )
    assert bad_part.last("clean") == cleaned_at
    assert hass.states.get("sensor.household_kohlefilter_bad_next_clean").state == "2026-07-01"

    # sending the same definitions again does not create the parts twice
    store.set_task_defs(store.data["task_defs"]["tasks"], [])
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert len([s for s in entry.subentries.values() if s.subentry_type == "part"]) == 4
