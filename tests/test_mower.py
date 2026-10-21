"""Robot mowers: subentry, area tracking, entities on the mower device, notifications."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.smarthome_ui.const import DOMAIN
from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er


def _mower_device(hass: HomeAssistant) -> str:
    """Create a fake Mammotion mower device with its entities."""
    other = MockConfigEntry(domain="mammotion")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("mammotion", "luba1")}, name="Luba"
    )
    ents = er.async_get(hass)

    def add(domain: str, uid: str, object_id: str, key: str | None, state: str, name: str) -> None:
        ents.async_get_or_create(
            domain,
            "mammotion",
            uid,
            device_id=device.id,
            suggested_object_id=object_id,
            translation_key=key,
            config_entry=other,
        )
        hass.states.async_set(f"{domain}.{object_id}", state, {"friendly_name": name})

    add("lawn_mower", "m", "luba", None, "docked", "Luba")
    add("switch", "a1", "luba_vorgarten", "area", "on", "Luba Bereich Vorgarten")
    add("switch", "a2", "luba_hinten", "area", "on", "Luba Bereich Hinten")
    add("sensor", "z", "luba_work_area", "work_area", "not working", "Luba Aktueller Ort")
    add("sensor", "c", "luba_self_check", "self_check", "ok", "Luba Selbsttest")
    add("sensor", "bu", "luba_blade_used", "blade_used_time", "50", "Luba Messer Nutzung")
    add("sensor", "bw", "luba_blade_warn", "blade_used_warn_time", "100", "Luba Messer Warnzeit")
    return device.id


async def _setup(
    hass: HomeAssistant, device_id: str, sources: dict | None = None
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={},
        title="Smarthome UI Core",
        subentries_data=[
            ConfigSubentryData(
                data={"device_id": device_id}, subentry_type="mower", title="Luba", unique_id=None
            )
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    if sources:
        entry.runtime_data.store.set_notify({"sources": sources})
        await hass.async_block_till_done()
    return entry


async def test_add_mower_subentry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device_id = _mower_device(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "mower"), context={"source": "user"}
    )
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device_id": device_id}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Luba"
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert [s.title for s in entry.subentries.values()] == ["Luba"]
    # reloaded with the mower: areas from the area switches
    assert entry.runtime_data.mowers.trackers
    assert hass.states.get("sensor.garden_vorgarten_last_mowed") is not None

    # the same device a second time → abort
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "mower"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device_id": device_id}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_tracks_areas(hass: HomeAssistant, notify_calls, freezer) -> None:
    freezer.move_to("2026-06-10 10:00:00")
    device_id = _mower_device(hass)
    entry = await _setup(hass, device_id)

    sensor = er.async_get(hass).async_get("sensor.garden_vorgarten_last_mowed")
    mower_device = dr.async_get(hass).async_get(device_id)
    assert sensor.device_id == mower_device.id  # on the mower's own device
    assert hass.states.get("sensor.garden_hinten_last_mowed").state == "unknown"

    # a job: per-area sensor with the mode as value
    hass.states.async_set("lawn_mower.luba", "mowing", {"friendly_name": "Luba"})
    ents = er.async_get(hass)
    ents.async_get_or_create(
        "sensor", "mammotion", "job", device_id=device_id, suggested_object_id="luba_vorgarten_job"
    )
    hass.states.async_set(
        "sensor.luba_vorgarten_job", "border", {"friendly_name": "Luba Vorgarten"}
    )
    await hass.async_block_till_done()
    state = hass.states.get("sensor.garden_vorgarten_last_mowed")
    assert state.attributes["mowing"] is True

    freezer.tick(timedelta(minutes=25))
    hass.states.async_set(
        "sensor.luba_vorgarten_job", "unavailable", {"friendly_name": "Luba Vorgarten"}
    )
    hass.states.async_set("lawn_mower.luba", "returning", {"friendly_name": "Luba"})
    await hass.async_block_till_done()
    log = entry.runtime_data.store.data["data"]["mow_log"]["Vorgarten"]
    assert log["mode"] == "Kanten"
    assert log["mins"] == 25
    assert log["mower"] == next(iter(entry.subentries))
    state = hass.states.get("sensor.garden_vorgarten_last_mowed")
    assert state.state != "unknown"
    assert state.attributes["mowing"] is False

    # only passing through (position, under 10 minutes) does not count
    hass.states.async_set("lawn_mower.luba", "mowing", {"friendly_name": "Luba"})
    hass.states.async_set("sensor.luba_work_area", "Luba Bereich Hinten", {"friendly_name": "x"})
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=3))
    hass.states.async_set("sensor.luba_work_area", "not working", {"friendly_name": "x"})
    await hass.async_block_till_done()
    assert hass.states.get("sensor.garden_hinten_last_mowed").state == "unknown"

    # dashboard data contains the mower
    assert entry.runtime_data.mowers.info()[0]["entity"] == "lawn_mower.luba"


async def test_mower_notifications(hass: HomeAssistant, notify_calls) -> None:
    device_id = _mower_device(hass)
    await _setup(hass, device_id, {"gd.mower": {"push": True}})

    hass.states.async_set("lawn_mower.luba", "error", {"friendly_name": "Luba"})
    await hass.async_block_till_done()
    assert [c.data["message"] for c in notify_calls] == ["Luba: Fehler"]

    hass.states.async_set("sensor.luba_blade_used", "100", {"friendly_name": "x"})
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Luba: Messer wechseln"


async def test_no_mower_rules_without_push(hass: HomeAssistant, notify_calls) -> None:
    device_id = _mower_device(hass)
    await _setup(hass, device_id)
    hass.states.async_set("lawn_mower.luba", "error", {"friendly_name": "Luba"})
    await hass.async_block_till_done()
    assert notify_calls == []
