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
from homeassistant.helpers.device_registry import DeviceEntry
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
from .displays import DisplayManager
from .mower import MowerManager
from .notify import NotifyEngine
from .planner import PlannerManager
from .store import CoreStore
from .tasks import find_task, task_states
from .vacuum import VacuumManager

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TODO,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class CoreRuntime:
    """Runtime data of the config entry."""

    store: CoreStore
    engine: NotifyEngine
    displays: DisplayManager
    mowers: MowerManager
    vacuums: VacuumManager
    planners: PlannerManager

    def robots(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return mowers and vacuums with their planner state (for the dashboard)."""
        mowers = [{**m, "planner": self.planners.info(m["id"])} for m in self.mowers.info()]
        vacuums = [{**v, "planner": self.planners.info(v["id"])} for v in self.vacuums.info()]
        return mowers, vacuums

    def tasks(self) -> list[dict[str, Any]]:
        """Return the tasks from the dashboard and from the integration (areas to mow)."""
        return [*self.store.data["tasks"], *self.mowers.tasks()]

    @callback
    def complete(self, key: str, who: str | None = None, label: str = "") -> None:
        """Mark a task done (an area to mow counts as mowed by hand)."""
        if not self.mowers.complete(key):
            self.store.mark_done(key, who=who, label=label)


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
        task = find_task(task_states(runtime.tasks(), store.done), value)
        if task is None and ":" not in value:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_task",
                translation_placeholders={"task": value},
            )
        key = task.key if task else value
        if call.service == SERVICE_TASK_DONE:
            runtime.complete(
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
        runtime = _runtime(hass)
        area = call.data[ATTR_AREA].strip()
        for tracker in runtime.mowers.trackers.values():
            if area.casefold() in (a.casefold() for a in tracker.areas):
                tracker.mark_mowed(
                    next(a for a in tracker.areas if a.casefold() == area.casefold())
                )
                return
        runtime.store.area_mowed(area)

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
        ws_display_connect,
        ws_display_update,
        ws_mower_settings,
        ws_cleaning_settings,
    ):
        websocket_api.async_register_command(hass, command)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: CoreConfigEntry) -> bool:
    """Set up from a config entry."""
    store = CoreStore(hass)
    await store.async_load()
    engine = NotifyEngine(hass, store)
    mowers = MowerManager(hass, store)
    vacuums = VacuumManager(hass, store)
    planners = PlannerManager(hass, store, engine)
    entry.runtime_data = CoreRuntime(
        store, engine, DisplayManager(hass, store), mowers, vacuums, planners
    )
    _remove_single_device(hass, entry)
    engine.extra_rules = lambda: [*mowers.rules(), *vacuums.rules()]
    engine.extra_tasks = mowers.tasks
    mowers.async_setup(list(entry.subentries.values()))
    vacuums.async_setup(list(entry.subentries.values()))
    planners.async_setup(list(entry.subentries.values()), mowers.trackers, vacuums.trackers)
    await engine.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # a mower added, changed or removed → set up again
    entry.async_on_unload(entry.add_update_listener(_async_reload))
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


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: CoreConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a display that is not open right now (it comes back when opened again)."""
    prefix = f"{entry.entry_id}_display_"
    for domain, ident in device.identifiers:
        if domain == DOMAIN and ident.startswith(prefix):
            display_id = ident.removeprefix(prefix)
            manager = entry.runtime_data.displays
            if manager.online(display_id):
                return False
            manager.remove(display_id)
            return True
    return False


async def _async_reload(hass: HomeAssistant, entry: CoreConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: CoreConfigEntry) -> bool:
    """Unload the config entry."""
    entry.runtime_data.engine.async_stop()
    entry.runtime_data.mowers.async_stop()
    entry.runtime_data.vacuums.async_stop()
    entry.runtime_data.planners.async_stop()
    entry.runtime_data.displays.async_shutdown()
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
        "mowers": runtime.robots()[0],
        "vacuums": runtime.robots()[1],
        "cleaning": store.data["cleaning"],
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
            websocket_api.event_message(
                msg["id"],
                {
                    "data": runtime.store.dashboard_data(),
                    "mowers": runtime.robots()[0],
                    "vacuums": runtime.robots()[1],
                    "cleaning": runtime.store.data["cleaning"],
                },
            )
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


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/display/connect",
        vol.Required("display"): str,
        vol.Required("name"): str,
        vol.Optional("user"): vol.Any(None, str),
        vol.Optional("state"): dict,
        vol.Optional("options"): dict,
    }
)
@callback
def ws_display_connect(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Register an open dashboard as display; commands for it arrive as events."""
    if (runtime := _ws_runtime(connection, msg)) is None:
        return

    @callback
    def send(command: dict[str, Any]) -> None:
        connection.send_message(websocket_api.event_message(msg["id"], command))

    manager = runtime.displays
    connection.subscriptions[msg["id"]] = manager.connect(
        msg["display"],
        msg["name"],
        send,
        user=msg.get("user"),
        state=msg.get("state"),
        options=msg.get("options"),
    )
    connection.send_result(msg["id"], {"name": manager.displays[msg["display"]]["name"]})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/display/update",
        vol.Required("display"): str,
        vol.Optional("user"): vol.Any(None, str),
        vol.Optional("state"): dict,
        vol.Optional("options"): dict,
    }
)
@callback
def ws_display_update(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the current state of a display."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.displays.update(
            msg["display"], user=msg.get("user"), state=msg.get("state"), options=msg.get("options")
        )
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/mower/settings",
        vol.Required("mower"): str,
        vol.Optional("plan"): vol.Any(None, {vol.Coerce(str): vol.Coerce(int)}),
        vol.Optional("interval"): vol.Any(None, vol.Coerce(int)),
        vol.Optional("freq"): vol.Any(None, {str: str}),
    }
)
@callback
def ws_mower_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change the mowing plan of a mower (own values per month, fixed interval, per area)."""
    if (runtime := _ws_runtime(connection, msg)) is None:
        return
    tracker = runtime.mowers.trackers.get(msg["mower"])
    if tracker is None:
        connection.send_error(msg["id"], "not_found", "Unknown mower")
        return
    tracker.update_settings({k: msg[k] for k in ("plan", "interval", "freq") if k in msg})
    connection.send_result(msg["id"], tracker.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/cleaning/settings",
        vol.Required("intervals"): {vol.Coerce(str): vol.Coerce(int)},
    }
)
@callback
def ws_cleaning_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Replace the own cleaning intervals per area (days, 0 = not tracked)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.store.set_cleaning_intervals(msg["intervals"])
        connection.send_result(msg["id"], runtime.store.data["cleaning"])
