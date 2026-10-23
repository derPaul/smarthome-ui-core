"""Device catalog: which device is the inverter, the battery, the heat pump, the gateway …

A device gets a role as subentry ("Gerät zuordnen": pick the device, pick the role). On the device
(and the devices connected through it) Core finds the entities of the role by their names, the
way the dashboard did it; single entities can be set by hand ("Entitäten anpassen").
UniFi devices without a subentry come in automatically (model: gateway / switch / access point).

The catalog is the one source for the dashboard (what to show) and for Core's own checks
(house technology, system & network). Step 1: house technology and network.

The dashboard's former settings are taken over once (``catalog/migrate``): for every role it used,
a subentry with the device of its entities.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .entity import all_devices, has_identifier
from .mower import CONF_DEVICE

SUBENTRY_DEVICE = "device"
CONF_ROLE = "role"
CONF_ENTITIES = "entities"
CONF_CAPACITY = "capacity"

ROLES = [
    "inverter",
    "battery",
    "heatpump",
    "water",
    "internet",
    "gateway",
    "switch",
    "ap",
    "server",
]
ROLE_LABELS = {
    "inverter": "Wechselrichter",
    "battery": "Batterie",
    "heatpump": "Wärmepumpe",
    "water": "Enthärter",
    "internet": "Internet-Router",
    "gateway": "Gateway",
    "switch": "Switch",
    "ap": "Accesspoint",
    "network": "Netzwerkgerät",
    "server": "Home-Assistant-Server",
}
SINGLE_ROLES = ("inverter", "battery", "heatpump", "water", "internet", "server")


@dataclass(frozen=True)
class Key:
    """How to find one entity of a role on its device."""

    domains: tuple[str, ...]
    pattern: str
    exclude: str | None = None
    units: tuple[str, ...] | None = None
    device_class: str | None = None
    numeric: bool | None = None


_W = ("W", "kW")
_NET = {
    "state": Key(("sensor",), r"(^|[\s_])(device_state|state|status|zustand)($|\s)", r"port_\d"),
    "uptime": Key(("sensor",), r"(device_uptime|uptime|betriebszeit|laufzeit)", r"port_\d"),
    "cpu": Key(("sensor",), r"cpu", r"temp"),
    "memory": Key(("sensor",), r"(memory|speicher|ram)"),
    "temperature": Key(("sensor",), r"temperat"),
    "clients": Key(("sensor",), r"clients?($|\s)"),
    "restart": Key(("button",), r"(restart|reboot|neustart|neu_starten)", r"port"),
    "update": Key(("update",), r"."),
}
KEYS: dict[str, dict[str, Key]] = {
    "inverter": {
        "pv_power": Key(
            ("sensor",),
            r"(pv|solar|dc).{0,20}(power|leistung)|erzeugung|produktion|input power|pv.?leistung",
            r"(grid|netz|batter|house|haus|load|last|export|import|max|peak|spitze|forecast|prognose)",
            _W,
        ),
        "error": Key(
            ("sensor", "binary_sensor"),
            r"(fehler|error|status ?message|statusnachricht|störung|fault)",
        ),
        "daily_energy": Key(
            ("sensor",),
            r"(today|heute|daily|tages).{0,25}(energy|energie|ertrag|yield|produc|erzeug)|"
            r"(energy|energie|ertrag|yield).{0,25}(today|heute|daily|tag)",
            None,
            ("kWh", "Wh"),
        ),
    },
    "battery": {
        "soc": Key(
            ("sensor",),
            r"(state of charge|ladezustand|ladestand|\bsoc\b|battery level|batteriestand)",
            r"(health|soh|min|max|reserve)",
            ("%",),
        ),
        "power": Key(
            ("sensor",),
            r"(active power|entladeleistung|batterieleistung|battery power|charge.?discharge power|"
            r"(^|\s)power($|\s)|leistung)",
            r"(grid|netz|apparent|schein|max|pv|solar)",
            _W,
        ),
        "state": Key(
            ("sensor",),
            r"(^|\s)(state|status|zustand|betriebszustand)($|\s)",
            None,
            None,
            None,
            False,
        ),
        "capacity_entity": Key(
            ("sensor",), r"(capacity|kapazität|kapazitat)", r"(remaining|rest|frei)", ("kWh", "Wh")
        ),
        "max_discharge": Key(("sensor",), r"max(imum|\.)? ?(discharg|entlade)", None, _W),
    },
    "heatpump": {
        "climate": Key(("climate",), r"."),
        "error": Key(("binary_sensor", "sensor"), r"(fehler|error|störung|fault)", r"(code|count)"),
        "mode_select": Key(("select",), r"(betriebsart|operating mode|programm|mode)"),
        "pressure": Key(
            ("sensor",),
            r"(heizdruck|heating pressure|heizungsdruck|anlagendruck|systemdruck|druck heizkreis|"
            r"heizkreis.*druck|wasserdruck|water pressure|system pressure)",
            r"(hochdruck|niederdruck|high|low|eingang|luftdruck|atmos)",
        ),
        "compressor": Key(("binary_sensor",), r"(verdichter|compressor)"),
        "heat_mode": Key(("binary_sensor",), r"(heizbetrieb|heating mode|heating active)"),
        "summer_mode": Key(("binary_sensor",), r"(sommerbetrieb|summer)"),
        "dhw": Key(
            ("sensor",),
            r"(ist.?temperatur warmwasser|warmwasser ist|warmwassertemperatur|dhw actual|"
            r"hot water temp|dhw temp)",
            r"(soll|target|set|min|max)",
        ),
        "dhw_target": Key(
            ("sensor", "number"),
            r"(soll.?temperatur warmwasser|warmwasser soll|dhw target|hot water target)",
        ),
        "power": Key(
            ("sensor",),
            r"(leistungsaufnahme|power consumption|elektrische leistung|electric power|"
            r"(^|\s)power($|\s))",
            r"(thermisch|thermal|wärmeleistung|heizleistung|heat output|erzeugt|max|peak)",
            _W,
        ),
    },
    "water": {
        "salt": Key(
            ("sensor",),
            r"(salzmenge|salt amount|salt level|getsv1|(^|\s)salz)",
            r"(vorrat|supply|getss)",
        ),
        "salt_select": Key(("select",), r"(salzmenge|salt)"),
        "state": Key(("sensor",), r"(regeneration status|regenerationsstatus|getrgs)"),
        "flow": Key(("sensor",), r"(wasserdurchfluss|durchfluss|flow|getflo)"),
        "alarm": Key(("sensor",), r"(letzte alarme|(^|\s)alarm|getala)"),
        "warn": Key(("sensor",), r"(warnung|warning|getwrn)"),
        "notif": Key(("sensor",), r"(benachrichtigung|notification|getnot)"),
    },
    "internet": {
        "connected": Key(
            ("binary_sensor",), r"(connect|verbind|wan.?status|internet|online)", None, None
        ),
        "download": Key(
            ("sensor",), r"(download|empfang|received|(^|\s)rx)", r"(total|gesamt|max)"
        ),
        "upload": Key(("sensor",), r"(upload|gesendet|sent|(^|\s)tx)", r"(total|gesamt|max)"),
        "external_ip": Key(("sensor",), r"(external.?ip|externe.?ip|public.?ip|wan.?ip)"),
        "uptime": Key(("sensor",), r"(uptime|betriebszeit|laufzeit)"),
        "latency": Key(("sensor",), r"(latency|latenz|ping)", None, ("ms",)),
    },
    "gateway": {
        **_NET,
        "wan_rx": Key(("sensor",), r"wan.{0,10}(rx|empfang|download)"),
        "wan_tx": Key(("sensor",), r"wan.{0,10}(tx|gesendet|senden|upload)"),
        "wan_ip": Key(("sensor",), r"wan.{0,6}ip"),
        "latency": Key(("sensor",), r"(latency|latenz|ping)"),
    },
    "switch": dict(_NET),
    "ap": dict(_NET),
    "network": dict(_NET),
    # Home Assistant itself: System Monitor (or the host's own integration)
    "server": {
        "cpu": Key(
            ("sensor",), r"(processor_use|prozessornutzung|processor usage|cpu.?usage|cpu.?load)"
        ),
        "memory": Key(
            ("sensor",), r"(memory_use_percent|arbeitsspeicher.*(auslastung|nutzung)|memory usage)"
        ),
        "disk": Key(
            ("sensor",), r"(disk_use_percent|disk usage|datenträger|festplatte|speicherplatz)"
        ),
        "cpu_temp": Key(
            ("sensor",), r"(processor_temperature|(processor|prozessor|cpu).?(temp|temperatur))"
        ),
    },
}
_UNIFI_GATEWAY = re.compile(r"^(udm|uxg|usg|ucg|udr|uck|ux|dream|gateway|cloud gateway)", re.I)
_UNIFI_SWITCH = re.compile(r"^(usw|us-|us8|us16|us24|us48|usl|switch|flex)", re.I)
_UNIFI_AP = re.compile(r"^(u6|u7|uap|ual|uacc|nanohd|ac-|ap|access point|ubb|uk-ultra|e7)", re.I)


def unifi_role(model: str | None) -> str | None:
    """Return gateway / switch / ap for a UniFi model name."""
    model = (model or "").strip()
    for role, pattern in (
        ("gateway", _UNIFI_GATEWAY),
        ("switch", _UNIFI_SWITCH),
        ("ap", _UNIFI_AP),
    ):
        if pattern.search(model):
            return role
    return None


class Catalog:
    """The roles of the devices: linked by subentry or found (UniFi)."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, unifi_auto: Callable[[], bool]
    ) -> None:
        """Initialize."""
        self.hass = hass
        self.entry = entry
        self.unifi_auto = unifi_auto

    # ---------------------------------------------------------------- entities of a device

    def device_entities(self, device_id: str) -> list[er.RegistryEntry]:
        """Return the entities of a device and of the devices connected through it."""
        devices = dr.async_get(self.hass)
        ids = {device_id}
        if (dev := devices.async_get(device_id)) and dev.via_device_id:
            ids.add(dev.via_device_id)
        ids |= {d.id for d in all_devices(self.hass) if d.via_device_id == device_id}
        registry = er.async_get(self.hass)
        out: list[er.RegistryEntry] = []
        for did in ids:
            out += [
                e
                for e in er.async_entries_for_device(registry, did)
                if not e.disabled_by and e.platform != "smarthome_ui"
            ]
        # the device itself first (a station or hub second)
        out.sort(key=lambda e: e.device_id != device_id)
        return out

    def _text(self, entry: er.RegistryEntry) -> str:
        state = self.hass.states.get(entry.entity_id)
        name = state.attributes.get("friendly_name", "") if state else ""
        parts = [
            entry.translation_key or "",
            str(entry.unique_id).split("-")[0] if entry.platform == "unifi" else "",
            entry.entity_id.split(".", 1)[1].replace("_", " "),
            entry.original_name or "",
            name,
        ]
        return " ".join(p for p in parts if p).lower()

    def _matches(self, entry: er.RegistryEntry, key: Key) -> bool:
        if entry.domain not in key.domains:
            return False
        text = self._text(entry)
        if not re.search(key.pattern, text) or (key.exclude and re.search(key.exclude, text)):
            return False
        state = self.hass.states.get(entry.entity_id)
        if key.units is not None:
            unit = state.attributes.get("unit_of_measurement") if state else None
            if unit not in key.units:
                return False
        if key.numeric is False and state is not None:
            try:
                float(state.state)
            except ValueError:
                return True
            return False
        return True

    def detect(self, role: str, device_id: str) -> dict[str, str]:
        """Return the entities of a role found on the device ({key: entity_id})."""
        entities = self.device_entities(device_id)
        found: dict[str, str] = {}
        for key, spec in KEYS.get(role, {}).items():
            hit = next((e.entity_id for e in entities if self._matches(e, spec)), None)
            if hit is None and role == "battery" and key == "soc":
                hit = next(
                    (
                        e.entity_id
                        for e in entities
                        if e.domain == "sensor"
                        and (e.original_device_class or e.device_class) == "battery"
                    ),
                    None,
                )
            if hit:
                found[key] = hit
        return found

    # ---------------------------------------------------------------- the catalog

    def links(self) -> list[ConfigSubentry]:
        """Return the "device" subentries."""
        return [s for s in self.entry.subentries.values() if s.subentry_type == SUBENTRY_DEVICE]

    def devices(self) -> list[dict[str, Any]]:
        """Return all devices with a role: linked ones and (if on) UniFi devices found."""
        out: list[dict[str, Any]] = []
        devices = dr.async_get(self.hass)
        linked: set[str] = set()
        for sub in self.links():
            device_id = sub.data.get(CONF_DEVICE)
            role = sub.data.get(CONF_ROLE)
            if not device_id or role not in ROLES or devices.async_get(device_id) is None:
                continue
            linked.add(device_id)
            entities = {**self.detect(role, device_id), **(sub.data.get(CONF_ENTITIES) or {})}
            if role == "server":
                entities = {**self._server_extras(), **entities}
            out.append(
                self._item(sub.subentry_id, role, device_id, sub.title, entities, "link", sub.data)
            )
        if not any(d["role"] == "server" for d in out) and (server := self._server_auto()):
            out.append(server)
        if self.unifi_auto():
            for device in all_devices(self.hass):
                if device.id in linked or not has_identifier(device, "unifi"):
                    continue
                # an unknown model is still watched (as network device), just not shown as tile
                role = unifi_role(device.model) or "network"
                entities = self.detect(role, device.id)
                if "state" in entities:
                    name = device.name_by_user or device.name or ROLE_LABELS[role]
                    out.append(
                        self._item(
                            f"unifi_{device.id}", role, device.id, name, entities, "auto", {}
                        )
                    )
        return out

    def _server_extras(self) -> dict[str, str]:
        """Return the Home Assistant update and the last backup (not on a device of their own)."""
        out: dict[str, str] = {}
        if self.hass.states.get("update.home_assistant_core_update") is not None:
            out["core_update"] = "update.home_assistant_core_update"
        backup = next(
            (
                s.entity_id
                for s in self.hass.states.async_all("sensor")
                if s.entity_id.startswith("sensor.backup_last_successful")
            ),
            None,
        )
        if backup:
            out["backup"] = backup
        return out

    def _server_auto(self) -> dict[str, Any] | None:
        """Return Home Assistant itself: the System Monitor device, the update and the backup."""
        monitor = next(
            (d for d in all_devices(self.hass) if has_identifier(d, "systemmonitor")),
            None,
        )
        entities = {
            **(self.detect("server", monitor.id) if monitor else {}),
            **self._server_extras(),
        }
        if not entities:
            return None
        return {
            "id": "server_auto",
            "role": "server",
            "device_id": monitor.id if monitor else None,
            "name": "Home Assistant",
            "entities": entities,
            "prefix": None,
            "source": "auto",
        }

    def _item(
        self,
        item_id: str,
        role: str,
        device_id: str,
        name: str,
        entities: dict[str, str],
        source: str,
        data: Any,
    ) -> dict[str, Any]:
        objects = [e.entity_id.split(".", 1)[1] for e in self.device_entities(device_id)]
        prefix = ""
        if len(objects) > 1:
            first, last = min(objects), max(objects)
            i = 0
            while i < min(len(first), len(last)) and first[i] == last[i]:
                i += 1
            prefix = first[:i]
            prefix = prefix[: prefix.rfind("_")] if "_" in prefix else ""
        item = {
            "id": item_id,
            "role": role,
            "device_id": device_id,
            "name": name,
            "entities": entities,
            "prefix": prefix if len(prefix) > 3 else None,
            "source": source,
        }
        if role == "battery" and data.get(CONF_CAPACITY):
            item["capacity"] = data[CONF_CAPACITY]
        return item

    def by_role(self, role: str) -> list[dict[str, Any]]:
        """Return the devices of one role."""
        return [d for d in self.devices() if d["role"] == role]

    def first(self, role: str) -> dict[str, Any] | None:
        """Return the (first) device of a role."""
        return next(iter(self.by_role(role)), None)

    # ---------------------------------------------------------------- for the checks

    def technik_overlay(self) -> dict[str, Any]:
        """Return the house technology sensors from the catalog (over the dashboard's)."""
        out: dict[str, Any] = {}
        reach: dict[str, str] = {}
        if inv := self.first("inverter"):
            e = inv["entities"]
            if e.get("error"):
                out["inverter_error"] = e["error"]
            if e.get("pv_power"):
                reach["Wechselrichter"] = e["pv_power"]
        if bat := self.first("battery"):
            e = bat["entities"]
            battery = {k: e[k] for k in ("power", "soc", "max_discharge") if e.get(k)}
            if bat.get("capacity"):
                battery["capacity"] = bat["capacity"]
            elif (cap := e.get("capacity_entity")) and (state := self.hass.states.get(cap)):
                try:
                    value = float(state.state)
                    unit = state.attributes.get("unit_of_measurement")
                    battery["capacity"] = value / 1000 if unit == "Wh" else value
                except ValueError:
                    pass
            if battery:
                out["battery"] = battery
            if e.get("soc"):
                reach["Batterie"] = e["soc"]
        if hp := self.first("heatpump"):
            e = hp["entities"]
            for key, target in (
                ("error", "heatpump_error"),
                ("pressure", "pressure"),
                ("compressor", "compressor"),
                ("heat_mode", "heat_mode"),
                ("summer_mode", "summer_mode"),
                ("dhw", "dhw"),
                ("dhw_target", "dhw_target"),
                ("power", "hp_power"),
            ):
                if e.get(key):
                    out[target] = e[key]
            if e.get("climate"):
                reach["Heizkreis"] = e["climate"]
        if water := self.first("water"):
            e = water["entities"]
            for key in ("salt", "flow"):
                if e.get(key):
                    out[key] = e[key]
            syr = {
                label: e[key]
                for key, label in (("alarm", "Alarm"), ("warn", "Warnung"), ("notif", "Hinweis"))
                if e.get(key)
            }
            if syr:
                out["syr"] = syr
        if reach:
            out["reach"] = reach
        return out

    def network_devices(self) -> list[dict[str, Any]]:
        """Return gateway, switches and access points for the system checks."""
        return [
            {
                "name": d["name"],
                "kind": d["role"],
                "entity": d["entities"].get("state"),
                "uptime": d["entities"].get("uptime"),
            }
            for d in self.devices()
            if d["role"] in ("gateway", "switch", "ap", "network") and d["entities"].get("state")
        ]

    def internet(self) -> str | None:
        """Return the internet connection entity of the router."""
        router = self.first("internet")
        return router["entities"].get("connected") if router else None

    # ---------------------------------------------------------------- linking

    @callback
    def link(
        self,
        device_id: str,
        role: str,
        name: str | None = None,
        entities: dict[str, str] | None = None,
    ) -> str:
        """Give a device a role (single roles: replaces the device of that role)."""
        if role not in ROLES:
            raise ValueError(role)
        detected = self.detect(role, device_id)
        own = {k: v for k, v in (entities or {}).items() if v and detected.get(k) != v}
        data = {CONF_DEVICE: device_id, CONF_ROLE: role, CONF_ENTITIES: own}
        device = dr.async_get(self.hass).async_get(device_id)
        title = (
            name or (device.name_by_user or device.name if device else None) or ROLE_LABELS[role]
        )
        for sub in self.links():
            same_device = sub.data.get(CONF_DEVICE) == device_id
            same_single = role in SINGLE_ROLES and sub.data.get(CONF_ROLE) == role
            if same_device or same_single:
                self.hass.config_entries.async_update_subentry(
                    self.entry, sub, data={**sub.data, **data}, title=title
                )
                return sub.subentry_id
        sub = ConfigSubentry(data=data, subentry_type=SUBENTRY_DEVICE, title=title, unique_id=None)
        self.hass.config_entries.async_add_subentry(self.entry, sub)
        return sub.subentry_id

    @callback
    def migrate(self, items: list[dict[str, Any]]) -> int:
        """Take over the dashboard's devices once: one subentry per device (with its role)."""
        registry = er.async_get(self.hass)
        linked = {s.data.get(CONF_DEVICE) for s in self.links()}
        made = 0
        for item in items:
            role = item.get("role")
            entities = {k: v for k, v in (item.get("entities") or {}).items() if v}
            anchors = [item.get("anchor"), *entities.values()]
            device_id = next(
                (
                    entry.device_id
                    for eid in anchors
                    if eid and (entry := registry.async_get(eid)) and entry.device_id
                ),
                None,
            )
            if role not in ROLES or not device_id or device_id in linked:
                continue
            if role in SINGLE_ROLES and any(s.data.get(CONF_ROLE) == role for s in self.links()):
                continue
            # only entities of this device count as own choice
            own_device = {e.entity_id for e in self.device_entities(device_id)}
            self.link(
                device_id,
                role,
                item.get("name"),
                {k: v for k, v in entities.items() if v in own_device},
            )
            linked.add(device_id)
            made += 1
        return made

    def info(self, migrated: bool) -> dict[str, Any]:
        """Return the catalog for the dashboard."""
        return {"migrated": migrated, "devices": self.devices(), "roles": ROLE_LABELS}
