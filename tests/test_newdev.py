"""Assistant for new devices: found, suggestions, applied."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)


async def test_new_device(hass: HomeAssistant, entry, hass_ws_client) -> None:
    newdev = entry.runtime_data.newdev
    assert newdev.info() == {"items": []}  # what was there before is not new
    office = ar.async_get(hass).async_create("Arbeitszimmer")
    matter = MockConfigEntry(domain="matter")
    matter.add_to_hass(hass)
    devs, ents = dr.async_get(hass), er.async_get(hass)
    dev = devs.async_get_or_create(
        config_entry_id=matter.entry_id,
        identifiers={("matter", "eve1")},
        name="Arbeitszimmer Eve Shutter Switch",
        manufacturer="Eve Systems",
        model="Eve Shutter Switch 20EBN9901",
    )
    cover = ents.async_get_or_create(
        "cover", "matter", "eve1_cover", device_id=dev.id, config_entry=matter
    )
    rssi = ents.async_get_or_create(
        "sensor",
        "matter",
        "eve1_rssi",
        device_id=dev.id,
        config_entry=matter,
        original_name="RSSI",
        entity_category=EntityCategory.DIAGNOSTIC,
        disabled_by=er.RegistryEntryDisabler.INTEGRATION,
    )
    await hass.async_block_till_done()
    items = newdev.info()["items"]
    assert len(items) == 1
    sug = items[0]
    assert sug["suggest_area"] == office.id
    assert sug["suggest_name"] == "Arbeitszimmer Rollladen"
    assert sug["diagnostics"] == [{"entity_id": rssi.entity_id, "name": "RSSI"}]
    assert sug["device_class"] == {cover.entity_id: "shutter"}
    assert sug["sim"] == [cover.entity_id]
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {
            "type": "smarthome_ui/newdev/apply",
            "device_id": dev.id,
            "area_id": office.id,
            "name": "Arbeitszimmer Rollladen",
            "enable": [rssi.entity_id],
            "device_class": {cover.entity_id: "shutter"},
            "sim": [cover.entity_id],
        }
    )
    res = await client.receive_json()
    assert res["success"] and res["result"] == {"items": []}
    dev = devs.async_get(dev.id)
    assert dev.area_id == office.id and dev.name_by_user == "Arbeitszimmer Rollladen"
    assert ents.async_get(rssi.entity_id).disabled_by is None
    assert ents.async_get(cover.entity_id).device_class == "shutter"
    assert cover.entity_id in entry.runtime_data.presence_sim.settings()["covers"]
    # another one, left as it is
    other = devs.async_get_or_create(
        config_entry_id=matter.entry_id, identifiers={("matter", "x2")}, name="Steckdose"
    )
    await hass.async_block_till_done()
    assert [i["device_id"] for i in newdev.info()["items"]] == [other.id]
    await client.send_json_auto_id({"type": "smarthome_ui/newdev/dismiss", "device_id": other.id})
    assert (await client.receive_json())["result"] == {"items": []}
