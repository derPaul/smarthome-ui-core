"""Smarthome UI Core: notifications and household data for the Smarthome UI dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.typing import ConfigType

from .const import (
    ATTR_AREA,
    ATTR_DATE,
    ATTR_MESSAGE,
    ATTR_TASK,
    DOMAIN,
    SERVICE_AREA_MOWED,
    SERVICE_TASK_DONE,
    SERVICE_TASK_REOPEN,
    SERVICE_TEST_NOTIFY,
    SERVICE_WASTE_ACK,
    SERVICE_WASTE_SKIP,
    SIGNAL_DATA,
)
from .notify import NotifyEngine
from .store import CoreStore
from .tasks import find_task, task_states

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.SWITCH, Platform.TODO]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class CoreRuntime:
    """Runtime data of the config entry."""

    store: CoreStore
    engine: NotifyEngine


type CoreConfigEntry = ConfigEntry[CoreRuntime]


def _runtime(hass: HomeAssistant) -> CoreRuntime:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="not_loaded")
    return entries[0].runtime_data


async def _user_name(hass: HomeAssistant, user_id: str | None) -> str | None:
    if not user_id:
        return None
    user = await hass.auth.async_get_user(user_id)
    return user.name if user else None


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions and the websocket API."""

    async def task_done(call: ServiceCall) -> None:
        runtime = _runtime(hass)
        store = runtime.store
        value = call.data[ATTR_TASK]
        task = find_task(task_states(store.data["tasks"], store.done), value)
        if task is None and ":" not in value:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_task",
                translation_placeholders={"task": value},
            )
        key = task.key if task else value
        if call.service == SERVICE_TASK_DONE:
            store.mark_done(
                key,
                who=await _user_name(hass, call.context.user_id),
                label=task.name if task else "",
            )
        else:
            store.reopen(key)

    for service in (SERVICE_TASK_DONE, SERVICE_TASK_REOPEN):
        hass.services.async_register(
            DOMAIN, service, task_done, schema=vol.Schema({vol.Required(ATTR_TASK): cv.string})
        )

    async def waste(call: ServiceCall) -> None:
        store = _runtime(hass).store
        day = call.data[ATTR_DATE].isoformat()
        skip = call.service == SERVICE_WASTE_SKIP
        store.mark_done(
            f"waste:{day}",
            by="skip" if skip else "hand",
            who=await _user_name(hass, call.context.user_id),
            label="Müll diesmal nicht rausgestellt" if skip else "Müll rausgestellt",
        )

    for service in (SERVICE_WASTE_ACK, SERVICE_WASTE_SKIP):
        hass.services.async_register(
            DOMAIN, service, waste, schema=vol.Schema({vol.Required(ATTR_DATE): cv.date})
        )

    async def area_mowed(call: ServiceCall) -> None:
        _runtime(hass).store.area_mowed(call.data[ATTR_AREA].strip())

    hass.services.async_register(
        DOMAIN,
        SERVICE_AREA_MOWED,
        area_mowed,
        schema=vol.Schema({vol.Required(ATTR_AREA): cv.string}),
    )

    async def test_notify(call: ServiceCall) -> None:
        engine = _runtime(hass).engine
        if not engine.send(
            "Smarthome",
            call.data.get(ATTR_MESSAGE) or "Test-Mitteilung von Smarthome UI Core",
            src="test",
            force=True,
        ):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="no_targets")

    hass.services.async_register(
        DOMAIN,
        SERVICE_TEST_NOTIFY,
        test_notify,
        schema=vol.Schema({vol.Optional(ATTR_MESSAGE): cv.string}),
    )

    for command in (
        ws_get,
        ws_subscribe,
        ws_import,
        ws_done,
        ws_data_set,
        ws_tasks_sync,
        ws_notify_config,
        ws_notify_report,
    ):
        websocket_api.async_register_command(hass, command)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CoreConfigEntry) -> bool:
    """Set up from a config entry."""
    store = CoreStore(hass)
    await store.async_load()
    engine = NotifyEngine(hass, store)
    entry.runtime_data = CoreRuntime(store, engine)
    _remove_single_device(hass, entry)
    await engine.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


@callback
def _remove_single_device(hass: HomeAssistant, entry: CoreConfigEntry) -> None:
    """Remove the one "Smarthome UI" device of the first preview (now one device per area)."""
    entities = er.async_get(hass)
    if old_todo := entities.async_get_entity_id("todo", DOMAIN, f"{entry.entry_id}_due"):
        entities.async_remove(old_todo)
    devices = dr.async_get(hass)
    if old := devices.async_get_device(identifiers={(DOMAIN, entry.entry_id)}):
        devices.async_remove_device(old.id)


async def async_unload_entry(hass: HomeAssistant, entry: CoreConfigEntry) -> bool:
    """Unload the config entry."""
    entry.runtime_data.engine.async_stop()
    await entry.runtime_data.store.async_flush()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


# -------------------------------------------------------------------- websocket API


def _ws_runtime(
    connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> CoreRuntime | None:
    entries = connection.hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        connection.send_error(msg["id"], "not_loaded", "Smarthome UI Core is not set up")
        return None
    return entries[0].runtime_data


def _state(runtime: CoreRuntime) -> dict[str, Any]:
    store = runtime.store
    return {
        "version": 1,
        "data": store.dashboard_data(),
        "migrated": store.data["migrated"],
        "notify": {
            "enabled": store.data["notify"].get("enabled", True),
            "rules": len(store.data["notify"].get("rules") or []),
            "targets": runtime.engine.targets(),
        },
    }


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/get"})
@callback
def ws_get(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return the dashboard data and status."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        connection.send_result(msg["id"], _state(runtime))


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/subscribe"})
@callback
def ws_subscribe(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Send the dashboard data whenever it changes."""
    if (runtime := _ws_runtime(connection, msg)) is None:
        return

    @callback
    def changed() -> None:
        connection.send_message(
            websocket_api.event_message(msg["id"], {"data": runtime.store.dashboard_data()})
        )

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_DATA, changed)
    connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/import",
        vol.Required("data"): dict,
        vol.Optional("force", default=False): bool,
    }
)
@callback
def ws_import(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take over the data the dashboard kept so far (once)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        imported = runtime.store.import_data(msg["data"], msg["force"])
        connection.send_result(msg["id"], {"imported": imported, **_state(runtime)})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/done",
        vol.Required("changes"): {str: vol.Any(None, dict)},
        vol.Optional("log", default=[]): [dict],
        vol.Optional("unlog", default=[]): [str],
    }
)
@callback
def ws_done(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Apply done changes (check off, reopen, undo)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.store.apply_done(msg["changes"], msg["log"], msg["unlog"])
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "smarthome_ui/data/set", vol.Required("data"): dict}
)
@callback
def ws_data_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replace data keys (household, mow_log)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.store.set_data(msg["data"])
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {vol.Required("type"): "smarthome_ui/tasks/sync", vol.Required("tasks"): [dict]}
)
@callback
def ws_tasks_sync(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Store the task definitions with their due dates."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.store.set_tasks(msg["tasks"])
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/notify/config",
        vol.Optional("rules"): [dict],
        vol.Optional("targets"): [str],
        vol.Optional("sources"): dict,
        vol.Optional("quiet"): vol.Any(None, dict),
        vol.Optional("task_hour"): vol.Any(None, vol.All(int, vol.Range(min=0, max=23))),
        vol.Optional("enabled"): bool,
    }
)
@callback
def ws_notify_config(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Store notification rules and settings."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.store.set_notify({k: v for k, v in msg.items() if k not in ("id", "type")})
        connection.send_result(msg["id"], _state(runtime)["notify"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/notify/report",
        vol.Required("client"): str,
        vol.Required("items"): [dict],
    }
)
@callback
def ws_notify_report(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the current items of an open dashboard."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.engine.report(msg["client"], msg["items"])
        connection.send_result(msg["id"])
