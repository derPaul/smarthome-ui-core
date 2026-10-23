"""Device catalog: roles by subentry, entities found on the device, migration, use by the checks."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr, entity_registry as er


def _device(
    hass: HomeAssistant, domain: str, name: str, entities: list[tuple], model: str = ""
) -> str:
    other = MockConfigEntry(domain=domain)
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={(domain, name)}, name=name, model=model
    )
    reg = er.async_get(hass)
    for ent_domain, object_id, state, attrs in entities:
        reg.async_get_or_create(
            ent_domain,
            domain,
            object_id,
            device_id=device.id,
            suggested_object_id=object_id,
            config_entry=other,
        )
        hass.states.async_set(f"{ent_domain}.{object_id}", state, attrs)
    return device.id


def _heatpump(hass: HomeAssistant) -> str:
    return _device(
        hass,
        "stiebel_eltron",
        "WP",
        [
            ("climate", "wp_heizkreis_1", "heat", {}),
            (
                "sensor",
                "wp_heizdruck",
                "1.8",
                {"unit_of_measurement": "bar", "friendly_name": "WP Heizdruck"},
            ),
            ("binary_sensor", "wp_verdichter", "off", {"friendly_name": "WP Verdichter"}),
            ("binary_sensor", "wp_heizbetrieb", "off", {"friendly_name": "WP Heizbetrieb"}),
            ("binary_sensor", "wp_fehlerstatus", "off", {"friendly_name": "WP Fehlerstatus"}),
            (
                "sensor",
                "wp_ist_temperatur_warmwasser",
                "48",
                {"friendly_name": "WP Ist-Temperatur Warmwasser"},
            ),
            (
                "sensor",
                "wp_leistungsaufnahme",
                "300",
                {"unit_of_measurement": "W", "friendly_name": "WP Leistungsaufnahme"},
            ),
        ],
    )


async def test_link_by_subentry_and_detect(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    device_id = _heatpump(hass)
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, "device"), context={"source": "user"}
    )
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"device_id": device_id, "role": "heatpump"}
    )
    assert result["step_id"] == "entities"
    # leave the found entities, but choose another pressure sensor by hand
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], {"pressure": "sensor.wp_ist_temperatur_warmwasser"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    catalog = entry.runtime_data.catalog
    hp = catalog.first("heatpump")
    e = hp["entities"]
    assert e["climate"] == "climate.wp_heizkreis_1"
    assert e["compressor"] == "binary_sensor.wp_verdichter"
    assert e["heat_mode"] == "binary_sensor.wp_heizbetrieb"
    assert e["error"] == "binary_sensor.wp_fehlerstatus"
    assert e["dhw"] == "sensor.wp_ist_temperatur_warmwasser"
    assert e["power"] == "sensor.wp_leistungsaufnahme"
    assert e["pressure"] == "sensor.wp_ist_temperatur_warmwasser"  # own choice wins
    assert hp["prefix"] is None  # too short to be a safe prefix
    # the house technology checks use it
    settings = entry.runtime_data.technik.settings()
    assert settings["heatpump_error"] == "binary_sensor.wp_fehlerstatus"
    assert settings["compressor"] == "binary_sensor.wp_verdichter"
    assert settings["reach"]["Heizkreis"] == "climate.wp_heizkreis_1"


async def test_migrate_battery_water_router(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    bat = _device(
        hass,
        "varta",
        "VARTA",
        [
            (
                "sensor",
                "varta_state_of_charge",
                "70",
                {"unit_of_measurement": "%", "friendly_name": "VARTA State of charge"},
            ),
            (
                "sensor",
                "varta_active_power",
                "-400",
                {"unit_of_measurement": "W", "friendly_name": "VARTA Active power"},
            ),
            (
                "sensor",
                "varta_installed_capacity",
                "13000",
                {"unit_of_measurement": "Wh", "friendly_name": "VARTA Installed capacity"},
            ),
        ],
    )
    _device(
        hass,
        "syr_connect",
        "SYR",
        [
            ("sensor", "syr_getsv1", "12", {"friendly_name": "Salzmenge"}),
            ("sensor", "syr_getflo", "0", {"friendly_name": "Wasserdurchfluss"}),
            ("sensor", "syr_getala", "no_alarm", {"friendly_name": "Alarm"}),
        ],
    )
    _device(
        hass,
        "fritz",
        "FRITZ!Box",
        [
            (
                "binary_sensor",
                "fritz_box_verbindung",
                "on",
                {"device_class": "connectivity", "friendly_name": "FRITZ!Box Verbindung"},
            )
        ],
    )
    client = await hass_ws_client(hass)
    items = [
        {
            "role": "battery",
            "anchor": "sensor.varta_state_of_charge",
            "entities": {"soc": "sensor.varta_state_of_charge"},
        },
        {"role": "water", "anchor": "sensor.syr_getsv1", "entities": {}},
        {"role": "internet", "anchor": "binary_sensor.fritz_box_verbindung", "entities": {}},
        {"role": "battery", "anchor": "sensor.unknown", "entities": {}},  # no device: skipped
    ]
    await client.send_json({"id": 1, "type": "smarthome_ui/catalog/migrate", "items": items})
    res = await client.receive_json()
    assert res["result"]["made"] == 3
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    catalog = entry.runtime_data.catalog
    assert {d["role"] for d in catalog.devices()} == {"battery", "water", "internet"}
    assert catalog.first("battery")["device_id"] == bat
    settings = entry.runtime_data.technik.settings()
    assert settings["battery"]["capacity"] == 13.0  # from the capacity sensor (Wh)
    assert settings["salt"] == "sensor.syr_getsv1"
    assert settings["syr"]["Alarm"] == "sensor.syr_getala"
    assert entry.runtime_data.system.internet() == "binary_sensor.fritz_box_verbindung"
    # only once
    await client.send_json({"id": 2, "type": "smarthome_ui/catalog/migrate", "items": items})
    assert (await client.receive_json())["result"]["made"] == 0


async def test_unifi_auto_and_link_from_dashboard(
    hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client
) -> None:
    sw = _device(
        hass,
        "unifi",
        "USW Pro",
        [("sensor", "usw_pro_state", "connected", {}), ("sensor", "usw_pro_uptime", "1", {})],
        model="USW-Pro-24-PoE",
    )
    catalog = entry.runtime_data.catalog
    found = catalog.first("switch")
    assert found["source"] == "auto" and found["entities"]["state"] == "sensor.usw_pro_state"
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "smarthome_ui/catalog/link",
            "device_id": sw,
            "role": "gateway",
            "name": "Keller",
        }
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    devices = entry.runtime_data.catalog.devices()
    assert [(d["role"], d["name"], d["source"]) for d in devices] == [("gateway", "Keller", "link")]


async def test_server_auto(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    other = MockConfigEntry(domain="systemmonitor")
    other.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=other.entry_id, identifiers={("systemmonitor", "pi")}, name="System Monitor"
    )
    reg = er.async_get(hass)
    for uid, obj, tkey in (
        ("cpu", "system_monitor_processor_use", "processor_use"),
        ("mem", "system_monitor_memory_use_percent", "memory_use_percent"),
    ):
        reg.async_get_or_create(
            "sensor",
            "systemmonitor",
            uid,
            device_id=device.id,
            suggested_object_id=obj,
            config_entry=other,
            translation_key=tkey,
        )
        hass.states.async_set(f"sensor.{obj}", "10", {})
    hass.states.async_set("update.home_assistant_core_update", "off", {})
    hass.states.async_set(
        "sensor.backup_last_successful_automatic_backup", "2026-10-20T03:00:00+00:00", {}
    )
    server = entry.runtime_data.catalog.first("server")
    assert server["source"] == "auto"
    assert server["entities"] == {
        "cpu": "sensor.system_monitor_processor_use",
        "memory": "sensor.system_monitor_memory_use_percent",
        "core_update": "update.home_assistant_core_update",
        "backup": "sensor.backup_last_successful_automatic_backup",
    }


async def test_page_lists(hass: HomeAssistant, entry: MockConfigEntry, hass_ws_client) -> None:
    client = await hass_ws_client(hass)
    await client.send_json(
        {
            "id": 1,
            "type": "smarthome_ui/lists/migrate",
            "page": "security",
            "lists": {"locks": [{"entity": "lock.haustur"}], "unknown": [1]},
        }
    )
    res = (await client.receive_json())["result"]
    assert res["made"] == 1 and res["pages"]["security"] == {"locks": [{"entity": "lock.haustur"}]}
    await client.send_json(
        {
            "id": 2,
            "type": "smarthome_ui/lists/set",
            "page": "security",
            "path": "cameras.entities",
            "items": [{"entity": "camera.einfahrt", "motion": "binary_sensor.einfahrt_person"}],
        }
    )
    res = (await client.receive_json())["result"]
    assert res["pages"]["security"]["cameras.entities"][0]["entity"] == "camera.einfahrt"
    await client.send_json(
        {
            "id": 3,
            "type": "smarthome_ui/lists/set",
            "page": "security",
            "path": "locks",
            "items": None,
        }
    )
    res = (await client.receive_json())["result"]
    assert "locks" not in res["pages"]["security"]  # automatic again
    await client.send_json(
        {"id": 4, "type": "smarthome_ui/lists/set", "page": "security", "path": "nope", "items": []}
    )
    assert (await client.receive_json())["success"] is False
    # migrated only once
    await client.send_json(
        {"id": 5, "type": "smarthome_ui/lists/migrate", "page": "security", "lists": {"locks": []}}
    )
    assert (await client.receive_json())["result"]["made"] == 0
