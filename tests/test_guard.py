"""Watcher: unclean restarts, hangs, low memory, Zigbee self-repair, database report."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er


def _messages(calls) -> list[str]:
    return [c.data["message"] for c in calls]


async def test_restart_hang_memory(hass: HomeAssistant, entry, notify_calls, monkeypatch) -> None:
    guard = entry.runtime_data.guard
    assert notify_calls == []  # first start: nothing to compare with
    # a clean stop is a normal restart
    guard.async_stop()
    assert guard.data["clean_stop"] is True
    guard.async_setup()
    assert guard.events[-1]["kind"] == "start" and guard.events[-1]["clean"] is True
    assert notify_calls == []
    # no clean stop: hung or crashed, gone for about 5 minutes
    guard.async_stop()
    guard.data.update({"clean_stop": False, "alive": guard.data["alive"] - 300_000})
    guard.async_setup()
    await hass.async_block_till_done()
    assert guard.events[-1]["clean"] is False and guard.events[-1]["down"] >= 299
    assert "ohne vorher sauber herunterzufahren – etwa 5 Min. weg" in _messages(notify_calls)[-1]
    # a timer that ran 45 s late: hang, told once in 6 hours
    monkeypatch.setattr("custom_components.smarthome_ui.guard.LAG_HANG_S", 30)
    guard.lag(2)
    guard.lag(45)
    guard.lag(50)
    await hass.async_block_till_done()
    assert [e["s"] for e in guard.events if e["kind"] == "hang"] == [45, 50]
    assert sum("nicht reagiert" in m for m in _messages(notify_calls)) == 1
    assert guard.data["hours"][-1]["lag"] == 50
    # memory almost gone
    monkeypatch.setattr("custom_components.smarthome_ui.guard.MEM_LOW_PCT", 5)
    guard.read_proc = lambda: {"total_mb": 3786, "avail_mb": 150, "avail_pct": 4.0, "load_pct": 80}
    await guard._async_sample()
    await hass.async_block_till_done()
    assert "Nur noch 150 MB Arbeitsspeicher frei (4 %)" in _messages(notify_calls)[-1]
    assert guard.data["hours"][-1]["mem"] == 4.0
    info = guard.info()
    assert info["week"]["unclean"] == 1 and info["week"]["hangs"] == 2
    assert "hang_told" not in {e["kind"] for e in info["events"]}


async def test_zigbee_self_repair(hass: HomeAssistant, entry, notify_calls) -> None:
    guard = entry.runtime_data.guard
    publish = async_mock_service(hass, "mqtt", "publish")
    mqtt = MockConfigEntry(domain="mqtt")
    mqtt.add_to_hass(hass)
    devs, ents = dr.async_get(hass), er.async_get(hass)
    bridge_dev = devs.async_get_or_create(
        config_entry_id=mqtt.entry_id, identifiers={("mqtt", "zigbee2mqtt_bridge_0xabc")}
    )
    ents.async_get_or_create(
        "binary_sensor",
        "mqtt",
        "bridge_conn",
        suggested_object_id="zigbee2mqtt_bridge_connection_state",
        device_id=bridge_dev.id,
        config_entry=mqtt,
    )
    hass.states.async_set("binary_sensor.zigbee2mqtt_bridge_connection_state", "on")
    for n, (ieee, state) in enumerate([("0x1", "unavailable"), ("0x2", "on"), ("0x3", "on")]):
        dev = devs.async_get_or_create(
            config_entry_id=mqtt.entry_id,
            identifiers={("mqtt", f"zigbee2mqtt_{ieee}")},
            name=f"Flur Lampe {n + 1}",
        )
        light = ents.async_get_or_create(
            "light", "mqtt", f"{ieee}_light", device_id=dev.id, config_entry=mqtt
        )
        hass.states.async_set(light.entity_id, state)
    info = guard.z2m()
    assert info["bridge"] == "binary_sensor.zigbee2mqtt_bridge_connection_state"
    assert info["devices"] == 3 and info["offline"] == 1 and info["names"] == ["Flur Lampe 1"]
    # as many as before the restart: nothing to repair
    guard._z2m_before = 1
    guard._heal_check("start", 0)
    await hass.async_block_till_done()
    assert publish == []
    # more than before: Zigbee2MQTT is restarted (once in 6 hours)
    guard._z2m_before = 0
    guard._heal_check("start", 0)
    await hass.async_block_till_done()
    assert [c.data["topic"] for c in publish] == ["zigbee2mqtt/bridge/request/restart"]
    assert _messages(notify_calls)[-1] == (
        "Zigbee2MQTT neu gestartet: 1 Gerät war nach dem Neustart von Home Assistant "
        "nicht erreichbar."
    )
    guard._heal_check("start", 0)
    await hass.async_block_till_done()
    assert len(publish) == 1
    # self-repair off: only noted
    guard.set_options({"heal": False})
    guard.data["events"] = [e for e in guard.events if e["kind"] != "heal"]
    guard._heal_check("start", 0)
    await hass.async_block_till_done()
    assert len(publish) == 1 and guard.events[-1]["kind"] == "heal_off"


async def test_db_report_only_when_calm(hass: HomeAssistant, entry, monkeypatch) -> None:
    """The automatic database report: weekly, not right after a start, not while busy."""
    from custom_components.smarthome_ui import guard as guard_mod

    guard = entry.runtime_data.guard
    guard.read_proc = lambda: {
        "total_mb": 3786,
        "avail_mb": 1400,
        "avail_pct": 37.0,
        "load_pct": 50,
    }
    hour = guard_mod.HOUR_MS
    # just started: not now
    assert not guard.db_due()
    guard.started -= 3 * hour
    assert guard.db_due()
    # a timer ran 3 s late a moment ago: wait
    guard.lag(3)
    assert not guard.db_due()
    guard._busy_at -= 20 * 60_000
    assert guard.db_due()
    # ran 2 days ago: not again before a week is over
    guard.data["db"] = {"at": guard_mod.now_ms() - 2 * guard_mod.DAY_MS}
    assert not guard.db_due()
    guard.data["db"] = {"at": guard_mod.now_ms() - 7 * guard_mod.DAY_MS}
    assert guard.db_due()
    # little memory left: not now
    guard.read_proc = lambda: {"total_mb": 3786, "avail_mb": 300, "avail_pct": 8.0, "load_pct": 50}
    assert not guard.db_due()
    called = []
    monkeypatch.setattr(guard, "async_db_scan", lambda: called.append(1) or _noop())
    guard._nightly(None)
    assert called == []


async def _noop() -> None:
    return None
