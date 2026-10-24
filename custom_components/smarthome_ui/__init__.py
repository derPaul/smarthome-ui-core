"""Smarthome UI Core: notifications and household data for the Smarthome UI dashboard."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.device_registry import DeviceEntry
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

from . import durations
from .alarm import AlarmLogic
from .appliance import ApplianceManager
from .catalog import ROLE_LABELS as CATALOG_LABELS, ROLES as CATALOG_ROLES, Catalog
from .const import (
    ATTR_AREA,
    ATTR_DATE,
    ATTR_MESSAGE,
    ATTR_TASK,
    CHORES_INTERVAL,
    DOMAIN,
    SERVICE_AREA_MOWED,
    SERVICE_TASK_DONE,
    SERVICE_TASK_REOPEN,
    SERVICE_TEST_NOTIFY,
    SERVICE_VACUUM_ROOMS,
    SERVICE_WASTE_ACK,
    SERVICE_WASTE_SKIP,
    SIGNAL_DATA,
    SIGNAL_TASKS,
)
from .displays import DisplayManager
from .doorbell import DoorbellManager
from .energy import EnergyPrefs
from .entity import entry_ids, find_device, has_identifier
from .forecast import Forecasts
from .grocery import Grocery
from .lights import LightSettings, suggest as suggest_lights, tag_automations
from .lists import PAGES as LIST_PAGES, VALUES as LIST_VALUES, PageLists
from .mailbox import Mailbox
from .maintenance import PartManager, async_migrate_tasks
from .mower import MowerManager
from .notify import NotifyEngine
from .planner import PlannerManager
from .plants import PlantManager
from .presence_sim import PresenceSim
from .security import Security
from .setup_devices import TYPES as SETUP_TYPES, add_device, suggest_devices
from .store import CoreStore
from .system import System
from .tasks import find_task, task_states
from .technik import Technik
from .ui import UiError, UiManager
from .vacuum import VacuumManager
from .waste import WasteManager
from .watchers import Watchers

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.TIME,
    Platform.TODO,
    Platform.UPDATE,
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
    waste: WasteManager
    watchers: Watchers
    appliances: ApplianceManager
    parts: PartManager
    plants: PlantManager
    security: Security
    doorbells: DoorbellManager
    alarm: AlarmLogic
    mailbox: Mailbox
    presence_sim: PresenceSim
    grocery: Grocery
    technik: Technik
    forecasts: Forecasts
    system: System
    catalog: Catalog
    lists: PageLists
    energy: EnergyPrefs
    ui: UiManager
    lights: LightSettings

    def robots(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Return mowers and vacuums with their planner state (for the dashboard)."""
        mowers = [{**m, "planner": self.planners.info(m["id"])} for m in self.mowers.info()]
        vacuums = [{**v, "planner": self.planners.info(v["id"])} for v in self.vacuums.info()]
        return mowers, vacuums

    def tasks(self) -> list[dict[str, Any]]:
        """Return the tasks from the dashboard and from the integration (areas to mow)."""
        return [
            *self.store.data["tasks"],
            *self.mowers.tasks(),
            *self.waste.tasks(),
            *self.watchers.tasks(),
            *self.appliances.tasks(),
            *self.parts.tasks(),
            *self.plants.tasks(),
        ]

    @callback
    def complete(self, key: str, who: str | None = None, label: str = "") -> None:
        """Mark a task done (an area to mow counts as mowed by hand)."""
        if not self.mowers.complete(key) and not self.plants.complete(key, who):
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

    async def vacuum_rooms(call: ServiceCall) -> ServiceResponse:
        runtime = _runtime(hass)
        entity = call.data[ATTR_ENTITY_ID]
        planner = next(
            (
                p
                for p in runtime.planners.planners.values()
                if p.kind == "vacuum" and p.tracker.entity_id == entity
            ),
            None,
        )
        if planner is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="unknown_vacuum",
                translation_placeholders={"entity": entity},
            )
        started = planner.vacuum_rooms(
            list(call.data["areas"]), call.data["skip_hours"], call.data["vacuum_only"]
        )
        return {"started": started}

    hass.services.async_register(
        DOMAIN,
        SERVICE_VACUUM_ROOMS,
        vacuum_rooms,
        schema=vol.Schema(
            {
                vol.Required(ATTR_ENTITY_ID): cv.entity_id,
                vol.Required("areas"): vol.All(cv.ensure_list, [cv.string]),
                vol.Optional("skip_hours", default=0): vol.Coerce(float),
                vol.Optional("vacuum_only", default=True): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
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
        ws_done,
        ws_data_set,
        ws_tasks_sync,
        ws_notify_config,
        ws_notify_report,
        ws_display_connect,
        ws_display_update,
        ws_mower_settings,
        ws_cleaning_settings,
        ws_task_defs,
        ws_waste_settings,
        ws_plant_settings,
        ws_security_settings,
        ws_alarm_settings,
        ws_mailbox_settings,
        ws_presence_sim_settings,
        ws_grocery_settings,
        ws_technik_settings,
        ws_system_settings,
        ws_catalog_link,
        ws_catalog_suggest,
        ws_catalog_unlink,
        ws_devices_suggest,
        ws_devices_add,
        ws_onboarding_done,
        ws_lists_set,
        ws_lists_value,
        ws_ui_check,
        ws_ui_install,
        ws_ui_notes,
        ws_lights_suggest,
        ws_lights_settings,
        ws_lights_ignore,
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
    waste = WasteManager(hass, store)
    watchers = Watchers(hass, store)
    appliances = ApplianceManager(hass, store)
    parts = PartManager(hass, store)
    plants = PlantManager(hass, store, watchers)

    @callback
    def security_notify(src: str, title: str, message: str, tag: str) -> None:
        """Open windows, everybody left, good night, last night (if push is on for src)."""
        cfg = (store.data["notify"].get("sources") or {}).get(src) or {}
        if cfg.get("push"):
            engine.send(
                title,
                message,
                src=src,
                url=cfg.get("url") or "/",
                urgent=bool(cfg.get("urgent")),
                tag=tag,
            )

    @callback
    def technik_notify(
        src: str,
        title: str,
        message: str,
        tag: str,
        urgent: bool = False,
        actions: list[dict[str, Any]] | None = None,
    ) -> None:
        """House technology: faults, pressure, water, base load (if push is on for src)."""
        cfg = (store.data["notify"].get("sources") or {}).get(src) or {}
        if cfg.get("push"):
            engine.send(
                title,
                message,
                src=src,
                url=cfg.get("url") or "/",
                urgent=urgent or bool(cfg.get("urgent")),
                tag=tag,
                actions=actions,
            )

    technik = Technik(hass, store, technik_notify)
    doorbells = DoorbellManager(hass, store)
    alarm = AlarmLogic(hass, store)

    @callback
    def doorbell_rang(tracker: Any) -> None:
        """Es klingelt – with the camera picture, urgent if nobody is home."""
        cfg = (store.data["notify"].get("sources") or {}).get("sec.bell") or {}
        if not cfg.get("push"):
            return
        nobody = not any(s.state == "home" for s in hass.states.async_all("person"))
        engine.send(
            "Klingel",
            f"Es klingelt: {tracker.name}",
            src="sec.bell",
            url=cfg.get("url") or "/",
            urgent=bool(cfg.get("urgent")) or nobody,
            tag=f"doorbell|{tracker.id}",
            camera=tracker.camera,
        )

    doorbells.on_ring = doorbell_rang
    security = Security(
        hass,
        store,
        watchers,
        security_notify,
        lambda: {t.device_id for t in appliances.trackers.values()},
    )
    entry.runtime_data = CoreRuntime(
        store,
        engine,
        DisplayManager(hass, store),
        mowers,
        vacuums,
        planners,
        waste,
        watchers,
        appliances,
        parts,
        plants,
        security,
        doorbells,
        alarm,
        Mailbox(hass, store, security_notify),
        PresenceSim(hass, store, lambda: alarm.panel or _first_panel(hass)),
        Grocery(hass, store),
        technik,
        Forecasts(hass, store, technik),
        System(hass, store, security_notify),
        Catalog(hass, entry, lambda: entry.runtime_data.system.options()["unifi_auto"]),
        PageLists(hass, store),
        EnergyPrefs(hass, store),
        UiManager(hass, store, dict(entry.options)),
        LightSettings(hass, store),
    )
    entry.runtime_data.system.catalog = entry.runtime_data.catalog
    security.alarm_reason = lambda: (
        r if alarm.configured and (r := alarm.reason()) != "unbekannt" else None
    )
    technik.overlay = entry.runtime_data.catalog.technik_overlay
    # maintenance tasks of the dashboard for filters, smoke detectors … become parts (once)
    async_migrate_tasks(hass, entry, store)
    _remove_single_device(hass, entry)
    _remove_duplicate_devices(hass, entry)
    await hass.async_add_executor_job(_install_blueprints, hass.config.path("blueprints"))
    engine.extra_rules = lambda: [
        *mowers.rules(),
        *vacuums.rules(),
        *waste.rules(),
        *watchers.rules(),
    ]
    engine.extra_tasks = lambda: [
        *mowers.tasks(),
        *watchers.notify_tasks(),
        *parts.tasks(),
        *plants.tasks(),
    ]

    @callback
    def unusual(tracker: Any, area: str, result: dict[str, Any]) -> None:
        """A run took clearly shorter or longer than usual: tell (if push is on for the robot)."""
        mower = tracker in mowers.trackers.values()
        src = "gd.mower" if mower else "hh.robot"
        cfg = (store.data["notify"].get("sources") or {}).get(src) or {}
        if not cfg.get("push"):
            return
        name = area if mower else tracker.area_name(area)
        engine.send(
            "Mähroboter" if mower else "Saugroboter",
            durations.message(tracker.name, name, result, mower),
            src=src,
            url=cfg.get("url") or "/",
            tag=f"duration|{tracker.id}|{area}",
        )

    mowers.on_unusual = unusual

    @callback
    def appliance_notify(tracker: Any, key: str, message: str) -> None:
        """Finished, error, reminder, solar power, fridge warnings (if push is on)."""
        cfg = (store.data["notify"].get("sources") or {}).get(tracker.source) or {}
        if not cfg.get("push"):
            return
        engine.send(
            tracker.label,
            message,
            src=tracker.source,
            url=cfg.get("url") or "/",
            urgent=bool(cfg.get("urgent")) or key == "alarm",
            tag=f"appliance|{tracker.id}|{key if key.startswith('supply|') else key.split('|')[0]}",
        )

    appliances.on_notify = appliance_notify
    vacuums.on_unusual = unusual
    mowers.async_setup(list(entry.subentries.values()))
    vacuums.async_setup(list(entry.subentries.values()))
    planners.async_setup(list(entry.subentries.values()), mowers.trackers, vacuums.trackers)
    waste.async_setup()
    watchers.async_setup()
    appliances.async_setup(list(entry.subentries.values()))
    parts.async_setup(entry)
    plants.async_setup()
    security.async_setup()
    doorbells.async_setup(list(entry.subentries.values()))
    alarm.async_setup()
    entry.runtime_data.mailbox.async_setup()

    # automations from Core's blueprints: label "Smart-Core" and a category (start, every reload)
    @callback
    def tag(*_args: Any) -> None:
        tag_automations(hass)

    entry.async_on_unload(hass.bus.async_listen("automation_reloaded", tag))
    entry.async_on_unload(async_at_started(hass, tag))
    # the dashboard card: served right away, GitHub checked a minute later
    await entry.runtime_data.ui.async_setup()
    entry.async_on_unload(entry.runtime_data.ui.async_start())
    # the energy dashboard's meters (does not hold up the start)
    hass.async_create_task(entry.runtime_data.energy.async_setup())
    entry.runtime_data.presence_sim.async_setup()
    entry.runtime_data.grocery.async_setup()
    entry.runtime_data.technik.async_setup()
    forecasts = entry.runtime_data.forecasts

    @callback
    def technik_settings() -> None:
        forecasts.listen()
        forecasts.update()

    technik.on_settings = technik_settings
    forecasts.async_setup()
    entry.runtime_data.system.async_setup()

    # tasks from the definitions: again after every change of the data and every quarter hour
    today: list[Any] = [dt_util.now().date()]

    @callback
    def recompute(*_args: Any) -> None:
        store.recompute_tasks()
        # a new day: due dates of parts, waste … move on (to-do lists, sensors)
        if (day := dt_util.now().date()) != today[0]:
            today[0] = day
            async_dispatcher_send(hass, SIGNAL_TASKS)

    recompute()
    entry.async_on_unload(async_dispatcher_connect(hass, SIGNAL_DATA, recompute))
    entry.async_on_unload(async_track_time_interval(hass, recompute, CHORES_INTERVAL))
    await engine.async_start()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # a mower added, changed or removed → set up again
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


def _install_blueprints(target: str) -> None:
    """Copy the blueprints that come with the integration (and keep them up to date)."""
    source = Path(__file__).parent / "blueprints"
    folder = Path(target) / "automation" / DOMAIN
    for file in source.glob("*.yaml"):
        dest = folder / file.name
        content = file.read_text(encoding="utf-8")
        if dest.exists() and dest.read_text(encoding="utf-8") == content:
            continue
        folder.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8")


@callback
def _remove_single_device(hass: HomeAssistant, entry: CoreConfigEntry) -> None:
    """Remove the one "Smarthome UI" device of the first preview (now one device per area)."""
    entities = er.async_get(hass)
    if old_todo := entities.async_get_entity_id("todo", DOMAIN, f"{entry.entry_id}_due"):
        entities.async_remove(old_todo)
    devices = dr.async_get(hass)
    if old := find_device(hass, DOMAIN, entry.entry_id):
        devices.async_remove_device(old.id)


@callback
def _remove_duplicate_devices(hass: HomeAssistant, entry: CoreConfigEntry) -> None:
    """Remove the empty copies of other integrations' devices that newer Home Assistant versions
    created for our entities (named "Smarthome UI Core"); the entities go to the real device."""
    devices = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(devices, entry.entry_id):
        if entry_ids(device) != {entry.entry_id}:
            continue
        if device.identifiers and not has_identifier(device, DOMAIN):
            devices.async_remove_device(device.id)


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: CoreConfigEntry, device: DeviceEntry
) -> bool:
    """Allow deleting a display that is not open right now (it comes back when opened again)."""
    prefix = f"{entry.entry_id}_display_"
    for identifier in device.identifiers:
        domain, ident = identifier[0], identifier[1]
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
    entry.runtime_data.ui.async_unload()
    entry.runtime_data.mowers.async_stop()
    entry.runtime_data.vacuums.async_stop()
    entry.runtime_data.planners.async_stop()
    entry.runtime_data.waste.async_stop()
    entry.runtime_data.watchers.async_stop()
    entry.runtime_data.appliances.async_stop()
    entry.runtime_data.plants.async_stop()
    entry.runtime_data.security.async_stop()
    entry.runtime_data.doorbells.async_stop()
    entry.runtime_data.alarm.async_stop()
    entry.runtime_data.mailbox.async_stop()
    entry.runtime_data.presence_sim.async_stop()
    entry.runtime_data.grocery.async_stop()
    entry.runtime_data.technik.async_stop()
    entry.runtime_data.forecasts.async_stop()
    entry.runtime_data.system.async_stop()
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


def _onboarding(store: Any) -> dict[str, Any]:
    """Return whether the setup assistant ran (an installation that already has data counts)."""
    data = store.data["onboarding"]
    if "done" in data:
        return {"done": bool(data["done"]), "at": data.get("at")}
    return {"done": bool(store.data["catalog_migrated"]), "at": None}


def _state(runtime: CoreRuntime) -> dict[str, Any]:
    store = runtime.store
    return {
        "version": 1,
        "data": store.dashboard_data(),
        "migrated": store.data["migrated"],
        "mowers": runtime.robots()[0],
        "vacuums": runtime.robots()[1],
        "cleaning": store.data["cleaning"],
        "appliances": runtime.appliances.info(),
        "parts": runtime.parts.info(),
        "parts_migrated": list(store.data.get("parts_migrated") or []),
        "plants": runtime.plants.info(),
        "security": runtime.security.info(),
        "doorbells": runtime.doorbells.info(),
        "alarm": runtime.alarm.info(),
        "mailbox": runtime.mailbox.info(),
        "presence_sim": runtime.presence_sim.info(),
        "grocery": runtime.grocery.info(),
        "technik": runtime.technik.info(),
        "forecast": runtime.forecasts.info(),
        "system": runtime.system.info(),
        "catalog": runtime.catalog.info(
            store.data["catalog_migrated"], store.data["catalog_steps"]
        ),
        "lists": runtime.lists.info(),
        "energy": runtime.energy.info(),
        "onboarding": _onboarding(store),
        "ui": runtime.ui.info(),
        "lights": runtime.lights.info(),
        "features": [
            "task_defs",
            "waste",
            "watchers",
            "appliances",
            "parts",
            "plants",
            "security",
            "doorbells",
            "alarm",
            "open_long",
            "appliance_supplies",
            "vacuum_station",
            "mailbox",
            "presence_sim",
            "grocery",
            "technik",
            "forecast",
            "system",
            "catalog",
            "lists",
            "onboarding",
            "energy",
            "setup_devices",
            "ui",
            "lights",
        ],
        "own_tasks": store.own_tasks,
        "waste": {
            "configured": runtime.waste.configured,
            "days": [
                {"date": d["date"].isoformat(), "kinds": d["kinds"]} for d in runtime.waste.days
            ],
        },
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
                    "appliances": runtime.appliances.info(),
                    "parts": runtime.parts.info(),
                    "parts_migrated": list(runtime.store.data.get("parts_migrated") or []),
                    "plants": runtime.plants.info(),
                    "security": runtime.security.info(),
                    "doorbells": runtime.doorbells.info(),
                    "alarm": runtime.alarm.info(),
                    "mailbox": runtime.mailbox.info(),
                    "presence_sim": runtime.presence_sim.info(),
                    "grocery": runtime.grocery.info(),
                    "technik": runtime.technik.info(),
                    "forecast": runtime.forecasts.info(),
                    "system": runtime.system.info(),
                    "catalog": runtime.catalog.info(
                        runtime.store.data["catalog_migrated"], runtime.store.data["catalog_steps"]
                    ),
                    "lists": runtime.lists.info(),
                    "energy": runtime.energy.info(),
                    "ui": runtime.ui.info(),
                    "onboarding": _onboarding(runtime.store),
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
        if not runtime.store.own_tasks:
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


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/tasks/defs",
        vol.Required("tasks"): [dict],
        vol.Required("rooms"): [dict],
    }
)
@callback
def ws_task_defs(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the task definitions (maintenance, garden calendar, cleaning rooms)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.store.set_task_defs(msg["tasks"], msg["rooms"])
        entries = hass.config_entries.async_loaded_entries(DOMAIN)
        if entries:
            async_migrate_tasks(hass, entries[0], runtime.store)
        connection.send_result(msg["id"], {"tasks": len(runtime.store.data["tasks"])})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/waste/settings",
        vol.Optional("calendars"): [str],
        vol.Optional("names"): {str: str},
        vol.Optional("remind"): dict,
    }
)
@callback
def ws_waste_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the waste settings (calendars, own names, reminder time)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.waste.set_settings(
            {k: msg[k] for k in ("calendars", "names", "remind") if k in msg}
        )
        connection.send_result(msg["id"])


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/plants/settings",
        vol.Required("entity"): str,
        vol.Optional("feed"): vol.Any(
            None,
            {
                vol.Required("every"): vol.All(vol.Coerce(int), vol.Range(min=0, max=52)),
                vol.Optional("from"): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
                vol.Optional("to"): vol.All(vol.Coerce(int), vol.Range(min=1, max=12)),
            },
        ),
    }
)
@callback
def ws_plant_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change the fertilizing calendar of a plant (every N weeks between two months)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.plants.set_settings(msg["entity"], msg.get("feed"))
        connection.send_result(msg["id"], runtime.plants.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/security/settings",
        vol.Optional("openings"): vol.Any(None, [str]),
        vol.Optional("night"): vol.Any(None, dict),
    }
)
@callback
def ws_security_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take which windows/doors and night sensors count (and the night times)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.security.set_settings({k: msg[k] for k in ("openings", "night") if k in msg})
        connection.send_result(msg["id"], runtime.security.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/alarm/settings",
        vol.Required("panel"): str,
        vol.Required("rules"): {str: [str]},
        vol.Required("actions"): dict,
        vol.Required("guests"): dict,
        vol.Required("nightwalk"): dict,
        vol.Optional("guests_on"): vol.Any(None, bool),
        vol.Optional("nightwalk_until"): vol.Any(None, int),
    }
)
@callback
def ws_alarm_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take over the alarm logic (rules, reaction, guests, night walk) from the dashboard."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.alarm.set_settings({k: v for k, v in msg.items() if k not in ("id", "type")})
        connection.send_result(msg["id"], runtime.alarm.info())


@callback
def _first_panel(hass: HomeAssistant) -> str | None:
    return next((s.entity_id for s in hass.states.async_all("alarm_control_panel")), None)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/mailbox/settings",
        vol.Optional("flap"): vol.Any(None, str),
        vol.Optional("door"): vol.Any(None, str),
        vol.Optional("enabled"): bool,
    }
)
@callback
def ws_mailbox_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the mailbox sensors chosen in the dashboard."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.mailbox.set_settings({k: msg[k] for k in ("flap", "door", "enabled") if k in msg})
        connection.send_result(msg["id"], runtime.mailbox.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/presence_sim/settings",
        vol.Optional("enabled"): bool,
        vol.Optional("lights"): [str],
        vol.Optional("covers"): [str],
        vol.Optional("start"): vol.In(["sunset", "time"]),
        vol.Optional("from"): str,
        vol.Optional("until"): str,
        vol.Optional("min"): vol.Coerce(int),
        vol.Optional("max"): vol.Coerce(int),
        vol.Optional("parallel"): vol.Coerce(int),
        vol.Optional("when"): vol.In(["away", "alarm", "always"]),
        vol.Optional("covers_open"): str,
    }
)
@callback
def ws_presence_sim_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the presence simulation from the dashboard (it replaces the old automation)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.presence_sim.set_settings({k: v for k, v in msg.items() if k not in ("id", "type")})
        connection.send_result(msg["id"], runtime.presence_sim.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/grocery/settings",
        vol.Required("entity"): vol.Any(None, str),
        vol.Optional("months"): vol.Any(None, {str: vol.Coerce(float)}),
    }
)
@callback
def ws_grocery_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the "last month" grocery sensor (and the months the dashboard kept so far)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.grocery.set_settings(msg["entity"], msg.get("months"))
        connection.send_result(msg["id"], runtime.grocery.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/technik/settings",
        vol.Optional("heatpump_error"): vol.Any(None, str),
        vol.Optional("inverter_error"): vol.Any(None, str),
        vol.Optional("inverter_error_ok"): vol.Any(None, [vol.Coerce(str)]),
        vol.Optional("reach"): {str: vol.Any(None, str)},
        vol.Optional("pressure"): vol.Any(None, str),
        vol.Optional("pressure_min"): vol.Coerce(float),
        vol.Optional("pressure_critical"): vol.Coerce(float),
        vol.Optional("pressure_drop"): vol.Coerce(float),
        vol.Optional("salt"): vol.Any(None, str),
        vol.Optional("salt_max"): vol.Coerce(float),
        vol.Optional("syr"): {str: vol.Any(None, str)},
        vol.Optional("flow"): vol.Any(None, str),
        vol.Optional("valve"): vol.Any(None, str),
        vol.Optional("water_minutes"): vol.Coerce(float),
        vol.Optional("compressor"): vol.Any(None, str),
        vol.Optional("cycles_per_hour"): vol.Coerce(float),
        vol.Optional("power"): dict,
        vol.Optional("battery"): dict,
        vol.Optional("dhw"): vol.Any(None, str),
        vol.Optional("dhw_target"): vol.Any(None, str),
        vol.Optional("hp_power"): vol.Any(None, str),
        vol.Optional("heat_mode"): vol.Any(None, str),
        vol.Optional("summer_mode"): vol.Any(None, str),
    }
)
@callback
def ws_technik_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take the house technology sensors the dashboard found or was told."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.technik.set_settings({k: v for k, v in msg.items() if k not in ("id", "type")})
        connection.send_result(msg["id"], runtime.technik.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/system/settings",
        vol.Optional("internet"): vol.Any(None, str),
        vol.Optional("devices"): [dict],
        vol.Optional("status"): [dict],
        vol.Optional("services"): [dict],
        vol.Optional("automations"): bool,
    }
)
@callback
def ws_system_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take what the dashboard watches on its system and network pages."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.system.set_settings({k: v for k, v in msg.items() if k not in ("id", "type")})
        connection.send_result(msg["id"], runtime.system.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/catalog/link",
        vol.Required("device_id"): str,
        vol.Required("role"): vol.In(CATALOG_ROLES),
        vol.Optional("name"): vol.Any(None, str),
    }
)
@callback
def ws_catalog_link(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Give a device a role (from the dashboard's "Gerät zuordnen")."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        sub = runtime.catalog.link(msg["device_id"], msg["role"], msg.get("name"))
        connection.send_result(msg["id"], {"subentry_id": sub})


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/catalog/suggest"})
@callback
def ws_catalog_suggest(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Candidate devices per role for the setup assistant."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        connection.send_result(
            msg["id"], {"roles": runtime.catalog.suggest(), "labels": CATALOG_LABELS}
        )


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/devices/suggest"})
@callback
def ws_devices_suggest(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Mowers, vacuums, appliances and doorbells found in Home Assistant (setup assistant)."""
    if _ws_runtime(connection, msg) is not None:
        entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
        connection.send_result(msg["id"], suggest_devices(hass, entry))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/devices/add",
        vol.Required("items"): [
            {
                vol.Required("type"): vol.In(SETUP_TYPES),
                vol.Required("device_id"): str,
                vol.Optional("name"): vol.Any(None, str),
                vol.Optional("kind"): vol.Any(None, str),
                vol.Optional("camera"): vol.Any(None, str),
                vol.Optional("lock"): vol.Any(None, str),
            }
        ],
    }
)
@callback
def ws_devices_add(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Add several of them at once (one reload afterwards, not one per device)."""
    if _ws_runtime(connection, msg) is None:
        return
    entry = hass.config_entries.async_loaded_entries(DOMAIN)[0]
    made = 0
    for it in msg["items"]:
        try:
            sub = add_device(
                hass,
                entry,
                it["type"],
                it["device_id"],
                it.get("name"),
                it.get("kind"),
                it.get("camera"),
                it.get("lock"),
            )
        except ValueError as err:
            connection.send_error(msg["id"], "invalid", str(err))
            return
        made += sub is not None
    connection.send_result(msg["id"], {"made": made})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/catalog/unlink",
        vol.Required("role"): vol.In(CATALOG_ROLES),
    }
)
@callback
def ws_catalog_unlink(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Remove the device of a role (no device for it)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        connection.send_result(msg["id"], {"removed": runtime.catalog.unlink(msg["role"])})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/onboarding/done",
        vol.Optional("done", default=True): bool,
    }
)
@callback
def ws_onboarding_done(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """The setup assistant finished (or is to run again: done = false)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.store.data["onboarding"] = {
            "done": msg["done"],
            "at": int(dt_util.utcnow().timestamp() * 1000),
        }
        runtime.store.save(SIGNAL_DATA)
        connection.send_result(msg["id"], _onboarding(runtime.store))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/catalog/migrate",
        vol.Required("items"): [dict],
        vol.Optional("step", default=1): vol.All(int, vol.Range(min=1)),
    }
)
@callback
def ws_catalog_migrate(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Take over the dashboard's device settings once per step (one device subentry per device)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        made = 0
        data = runtime.store.data
        if msg["step"] == 1:
            if not data["catalog_migrated"]:
                data["catalog_migrated"] = True
                runtime.store.save(SIGNAL_DATA)
                made = runtime.catalog.migrate(msg["items"])
        elif msg["step"] not in data["catalog_steps"]:
            data["catalog_steps"] = [*data["catalog_steps"], msg["step"]]
            runtime.store.save(SIGNAL_DATA)
            made = runtime.catalog.migrate(msg["items"])
        connection.send_result(msg["id"], {"made": made})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/lists/set",
        vol.Required("page"): vol.In(list(LIST_PAGES)),
        vol.Required("path"): str,
        vol.Required("items"): vol.Any(None, list),
    }
)
@callback
def ws_lists_set(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Set one list of a page (cameras, windows, locks …; None = automatic)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        try:
            runtime.lists.set(msg["page"], msg["path"], msg["items"])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid", str(err))
            return
        connection.send_result(msg["id"], runtime.lists.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/lists/value",
        vol.Required("path"): vol.In(list(LIST_VALUES)),
        vol.Required("value"): vol.Any(None, str),
    }
)
@callback
def ws_lists_value(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Set one single entity (weather, music player, cinema …; None = automatic)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.lists.set_value(msg["path"], msg["value"])
        connection.send_result(msg["id"], runtime.lists.info())


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/ui/check"})
@websocket_api.async_response
async def ws_ui_check(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Look for a new version of the dashboard (with its release notes)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        info = await runtime.ui.async_check()
        connection.send_result(msg["id"], {**info, "notes": runtime.ui.notes()})


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/ui/install"})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_ui_install(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Install the newest dashboard."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        try:
            info = await runtime.ui.async_install()
        except UiError as err:
            connection.send_error(msg["id"], "ui_failed", str(err))
            return
        connection.send_result(msg["id"], info)


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/ui/notes"})
@callback
def ws_ui_notes(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return what is new since the installed dashboard (and the state)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        connection.send_result(msg["id"], {**runtime.ui.info(), "notes": runtime.ui.notes()})


@websocket_api.websocket_command({vol.Required("type"): "smarthome_ui/lights/suggest"})
@callback
def ws_lights_suggest(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Return rooms with motion sensors and lights (and the automation that covers them)."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        tag_automations(hass)
        connection.send_result(
            msg["id"],
            {
                "rooms": suggest_lights(hass, runtime.lights.ignored()),
                "settings": runtime.lights.info(),
            },
        )


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/lights/settings",
        vol.Optional("day"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("night"): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
        vol.Optional("night_start"): str,
        vol.Optional("night_end"): str,
    }
)
@callback
def ws_lights_settings(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Change the central brightness and night times of the motion lights."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        try:
            for key in ("day", "night", "night_start", "night_end"):
                if key in msg:
                    runtime.lights.set(key, msg[key])
        except ValueError as err:
            connection.send_error(msg["id"], "invalid", str(err))
            return
        connection.send_result(msg["id"], runtime.lights.info())


@websocket_api.websocket_command(
    {
        vol.Required("type"): "smarthome_ui/lights/ignore",
        vol.Required("area_id"): str,
        vol.Optional("ignore", default=True): bool,
    }
)
@callback
def ws_lights_ignore(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Hide (or show again) the motion light suggestion for a room."""
    if (runtime := _ws_runtime(connection, msg)) is not None:
        runtime.lights.ignore(msg["area_id"], msg["ignore"])
        connection.send_result(msg["id"], {"rooms": suggest_lights(hass, runtime.lights.ignored())})
