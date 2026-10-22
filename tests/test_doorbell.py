"""Doorbells: subentry, ring with camera picture, mute switch, last ring."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util


def _doorbell(hass: HomeAssistant) -> str:
    other = MockConfigEntry(domain="unifiprotect")
    other.add_to_hass(hass)
    area = ar.async_get(hass).async_get_or_create("Eingang")
    devices = dr.async_get(hass)
    device = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("unifiprotect", "g6")}, name="G6 Entry"
    )
    devices.async_update_device(device.id, area_id=area.id)
    registry = er.async_get(hass)
    for domain, object_id, state, attrs in (
        ("event", "g6_doorbell", "unknown", {"device_class": "doorbell", "event_types": ["ring"]}),
        ("camera", "g6_package_camera", "idle", {}),
        ("camera", "g6_high_resolution_channel", "idle", {}),
        ("binary_sensor", "g6_person_detected", "off", {"device_class": "occupancy"}),
    ):
        registry.async_get_or_create(
            domain,
            "unifiprotect",
            object_id,
            device_id=device.id,
            suggested_object_id=object_id,
            config_entry=other,
        )
        hass.states.async_set(f"{domain}.{object_id}", state, attrs)
    # a lock in the same area (other device)
    lock_dev = devices.async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("unifiprotect", "lock")}, name="Schloss"
    )
    devices.async_update_device(lock_dev.id, area_id=area.id)
    registry.async_get_or_create(
        "lock",
        "unifiprotect",
        "lock",
        device_id=lock_dev.id,
        suggested_object_id="haustur",
        config_entry=other,
    )
    hass.states.async_set("lock.haustur", "locked", {})
    return device.id


async def test_doorbell(hass: HomeAssistant, entry: MockConfigEntry, notify_calls) -> None:
    device_id = _doorbell(hass)
    hass.states.async_set("person.anna", "not_home", {})
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "doorbell"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device_id": device_id, "name": "Haustür"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    entry.runtime_data.store.set_notify({"sources": {"sec.bell": {"push": True}}})
    await hass.async_block_till_done()

    info = entry.runtime_data.doorbells.info()[0]
    assert info["ring"] == "event.g6_doorbell"
    assert info["camera"] == "camera.g6_high_resolution_channel"
    assert info["lock"] == "lock.haustur"
    registry = er.async_get(hass)
    assert registry.async_get("sensor.security_haustur_last_ring").device_id == device_id
    assert registry.async_get("switch.security_haustur_notify").device_id == device_id

    now = dt_util.utcnow()
    hass.states.async_set("event.g6_doorbell", now.isoformat(), {"device_class": "doorbell"})
    await hass.async_block_till_done()
    assert notify_calls[-1].data["message"] == "Es klingelt: Haustür"
    data = notify_calls[-1].data["data"]
    assert data["image"] == "/api/camera_proxy/camera.g6_high_resolution_channel"
    assert data["push"]["interruption-level"] == "time-sensitive"  # urgent: nobody home

    # muted: remembered, but no notification
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.security_haustur_notify"}, blocking=True
    )
    count = len(notify_calls)
    hass.states.async_set(
        "event.g6_doorbell", (now + timedelta(seconds=30)).isoformat(), {"device_class": "doorbell"}
    )
    await hass.async_block_till_done()
    assert len(notify_calls) == count
    state = hass.states.get("sensor.security_haustur_last_ring")
    assert state.attributes["today"] == 2
