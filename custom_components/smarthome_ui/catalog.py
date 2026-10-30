"""Device catalog: which device is the inverter, the battery, the heat pump, the gateway …

A device gets a role as subentry ("Gerät zuordnen": pick the device, pick the role). On the device
(and the devices connected through it) Core finds the entities of the role by their names, the
way the dashboard did it; single entities can be set by hand ("Entitäten anpassen").
UniFi devices without a subentry come in automatically (model: gateway / switch / access point).

The catalog is the one source for the dashboard (what to show) and for Core's own checks
(house technology, system & network). Step 1: house technology and network; step 2: the Home
Assistant server; step 3: weather station, coffee machine, TV, player and sync box (home, rooms,
garden, household). TV, player and sync box are found by their integration, the weather station
by its sensors (wind, rain).
"""

from __future__ import annotations

from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import (
    area_registry as ar,
    device_registry as dr,
    entity_registry as er,
)

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
    "weather_station",
    "coffee",
    "tv",
    "player",
    "syncbox",
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
    "weather_station": "Wetterstation",
    "coffee": "Kaffeemaschine",
    "tv": "Fernseher",
    "player": "Zuspieler",
    "syncbox": "Sync-Box",
}
SINGLE_ROLES = (
    "inverter",
    "battery",
    "heatpump",
    "water",
    "internet",
    "server",
    "weather_station",
    "coffee",
    "tv",
    "player",
    "syncbox",
)
# found by their integration (the platform of their media player / switch) without a subentry
AUTO_PLATFORMS = {
    "tv": ("media_player", ("webostv", "samsungtv", "braviatv", "philips_js", "androidtv_remote")),
    "player": ("media_player", ("apple_tv", "androidtv", "nvidia_shield")),
    "syncbox": ("switch", ("huesyncbox",)),
}


@dataclass(frozen=True)
class Key:
    """How to find one entity of a role on its device."""

    domains: tuple[str, ...]
    pattern: str
    exclude: str | None = None
    units: tuple[str, ...] | None = None
    device_class: tuple[str, ...] | None = None
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
        # remaining capacity in litres: counts down with every litre (the water usage sensor)
        "capacity": Key(
            ("sensor",),
            r"(restkapazität|remaining capacity|getres)",
            r"(harz|resin|getcs|%)",
        ),
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
    "weather_station": {
        "temperature": Key(
            ("sensor",),
            r".",
            r"(dew|taupunkt|feels|gefühlt|gefuehlt|wind.?chill|heat.?index|innen|indoor|min|max)",
            None,
            ("temperature",),
        ),
        "humidity": Key(("sensor",), r".", r"(innen|indoor|absolute|absolut)", None, ("humidity",)),
        "pressure": Key(("sensor",), r".", None, None, ("atmospheric_pressure", "pressure")),
        "rain": Key(
            ("sensor",),
            r".",
            r"(rate|intensit|stunde|hour|heute|today|daily|tag|week|woche|month|monat|year|jahr|event)",
            None,
            ("precipitation",),
        ),
        "wind_speed": Key(("sensor",), r".", r"(gust|böe|boe|max)", None, ("wind_speed",)),
        "wind_gust": Key(("sensor",), r"(gust|böe|boe)", r"max", None, ("wind_speed",)),
        "wind_direction": Key(
            ("sensor",), r"(wind|richtung|direction|bearing)", r"(text|cardinal)", ("°",)
        ),
        "radiation": Key(("sensor",), r"(strahlung|radiation|solar|irradi)", r"(uv|lux)"),
    },
    "coffee": {
        "power": Key(("sensor",), r".", r"(max|peak|apparent|schein)", _W),
        "switch": Key(("switch",), r".", r"(led|child|kinder|lock|sperre)"),
        "energy": Key(("sensor",), r".", r"(today|heute|daily|tag)", ("kWh", "Wh")),
    },
    "tv": {"media": Key(("media_player",), r".")},
    "player": {"media": Key(("media_player",), r".")},
    "syncbox": {
        "sync": Key(("switch",), r"(light_sync|lichtsync|sync$|sync\s)", r"(power|strom)"),
        "power": Key(("switch",), r"(power|strom)"),
        "mode": Key(("select",), r"(sync.?mode|modus|mode)"),
        "intensity": Key(("select",), r"(intensity|intensit)"),
    },
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
# Suggestions (setup assistant): the entities a device needs at least to qualify for a role –
# all keys of one group (any group will do)
SUGGEST_NEEDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "inverter": (("pv_power",),),
    "battery": (("soc", "power"),),
    "heatpump": (
        ("climate", "compressor"),
        ("climate", "pressure"),
        ("climate", "dhw"),
        ("climate", "heat_mode"),
    ),
    "water": (("salt",), ("state", "flow")),
    "internet": (("connected", "download"), ("connected", "external_ip")),
    "weather_station": (("temperature", "wind_speed"), ("temperature", "rain")),
    "coffee": (("power",),),
}
# only devices whose name fits
SUGGEST_NAME = {
    "coffee": re.compile(
        r"(kaffee|coffee|espresso|siebtr(ä|ae)ger|vollautomat|barista|jura|delonghi|de.?longhi|"
        r"melitta|saeco|gaggia|rancilio|lelit|rocket)",
        re.I,
    ),
}
# outdoor sensors without a weather station: in an outdoor area first, else by name
_OUTDOOR_AREA = re.compile(r"(garten|garden|terrasse|balkon|außen|aussen|outdoor)", re.I)
_OUTDOOR_NAME = re.compile(
    r"(aussen|außen|outdoor|outside|garten|garden|terrasse|balkon|wetter|weather)", re.I
)
_OUTDOOR_NOT = re.compile(r"(dew|taupunkt|feels|gefühlt|apparent|boden|soil|pool|min|max)", re.I)
OUTDOOR_CLASSES = {
    "temperature": ("temperature",),
    "humidity": ("humidity",),
    "pressure": ("atmospheric_pressure", "pressure"),
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
        # the catalog of this turn of the event loop: entities writing their state together
        # (house technology, system) share one search instead of each running it again
        self._cached: tuple[tuple[Any, ...], list[dict[str, Any]]] | None = None

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
        if key.device_class is not None:
            device_class = entry.device_class or entry.original_device_class
            if device_class is None and state is not None:
                device_class = state.attributes.get("device_class")
            if device_class not in key.device_class:
                return False
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
        # a changed setting, link or registry counts at once (same turn included)
        key = (
            self.unifi_auto(),
            tuple(self.entry.subentries),
            len(dr.async_get(self.hass).devices),
            len(er.async_get(self.hass).entities),
        )
        if self._cached is None or self._cached[0] != key:
            if self._cached is None:
                self.hass.loop.call_soon(self._forget)
            self._cached = (key, self._devices())
        return deepcopy(self._cached[1])

    def _forget(self) -> None:
        self._cached = None

    def _devices(self) -> list[dict[str, Any]]:
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
        roles = {d["role"] for d in out}
        for role in AUTO_PLATFORMS:
            if role not in roles and (found := self._platform_auto(role, linked)):
                out.append(found)
        if "weather_station" not in roles and (station := self._station_auto(linked)):
            out.append(station)
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

    def _platform_auto(self, role: str, linked: set[str]) -> dict[str, Any] | None:
        """Return the first device of a TV / player / sync box integration (no subentry)."""
        domain, platforms = AUTO_PLATFORMS[role]
        registry = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        for entry in sorted(registry.entities.values(), key=lambda e: e.entity_id):
            if (
                entry.domain != domain
                or entry.platform not in platforms
                or entry.disabled_by
                or entry.hidden_by
                or not entry.device_id
                or entry.device_id in linked
                or (device := devices.async_get(entry.device_id)) is None
            ):
                continue
            entities = self.detect(role, device.id)
            if entities:
                name = device.name_by_user or device.name or ROLE_LABELS[role]
                return self._item(f"{role}_auto", role, device.id, name, entities, "auto", {})
        return None

    def _station_auto(self, linked: set[str]) -> dict[str, Any] | None:
        """Return a weather station: a device with wind or rain and a temperature."""
        registry = er.async_get(self.hass)
        candidates = {
            e.device_id
            for e in registry.entities.values()
            if e.domain == "sensor"
            and e.device_id
            and not e.disabled_by
            and (e.device_class or e.original_device_class) in ("wind_speed", "precipitation")
        }
        for device in all_devices(self.hass):
            if device.id in linked or device.id not in candidates:
                continue
            entities = self.detect("weather_station", device.id)
            if "temperature" in entities and ("wind_speed" in entities or "rain" in entities):
                name = device.name_by_user or device.name or ROLE_LABELS["weather_station"]
                return self._item(
                    "weather_station_auto", "weather_station", device.id, name, entities, "auto", {}
                )
        return None

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
        first: dict[str, dict[str, Any]] = {}
        for device in self.devices():
            first.setdefault(device["role"], device)
        if inv := first.get("inverter"):
            e = inv["entities"]
            if e.get("error"):
                out["inverter_error"] = e["error"]
            if e.get("pv_power"):
                reach["Wechselrichter"] = e["pv_power"]
        if bat := first.get("battery"):
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
        if hp := first.get("heatpump"):
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
        if water := first.get("water"):
            e = water["entities"]
            for key in ("salt", "flow", "capacity"):
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

    # ---------------------------------------------------------------- suggestions (onboarding)

    def suggest(self) -> dict[str, list[dict[str, Any]]]:
        """Return candidate devices per role, best first (for the setup assistant)."""
        devices = all_devices(self.hass)
        registry = er.async_get(self.hass)
        own = {
            e.device_id
            for e in registry.entities.values()
            if e.platform == "smarthome_ui" and e.device_id
        }
        current = {d["role"]: d.get("device_id") for d in self.devices()}
        out: dict[str, list[dict[str, Any]]] = {}
        for role in ROLES:
            found: list[dict[str, Any]] = []
            if role in AUTO_PLATFORMS:
                domain, platforms = AUTO_PLATFORMS[role]
                ids = {
                    e.device_id
                    for e in registry.entities.values()
                    if e.domain == domain
                    and e.platform in platforms
                    and e.device_id
                    and not e.disabled_by
                }
                if role == "tv":
                    # other TVs: media players of device class tv
                    ids |= {
                        e.device_id
                        for e in registry.entities.values()
                        if e.domain == "media_player"
                        and (e.device_class or e.original_device_class) == "tv"
                        and e.device_id
                    }
                pool = [d for d in devices if d.id in ids]
            elif role in SUGGEST_NEEDS:
                pool = devices
            else:
                pool = []
            # the device assigned now is always among the candidates
            if (now := current.get(role)) and all(d.id != now for d in pool):
                pool = [*(d for d in devices if d.id == now), *pool]
            for device in pool:
                if device.id in own:
                    continue
                name = device.name_by_user or device.name or ""
                entities = self.detect(role, device.id)
                if device.id != current.get(role):
                    if (pattern := SUGGEST_NAME.get(role)) and not pattern.search(name):
                        continue
                    needs = SUGGEST_NEEDS.get(role)
                    if needs and not any(all(k in entities for k in group) for group in needs):
                        continue
                    if not entities:
                        continue
                found.append(
                    {
                        "device_id": device.id,
                        "name": name or ROLE_LABELS[role],
                        "manufacturer": device.manufacturer,
                        "model": device.model,
                        "area": device.area_id,
                        "entities": entities,
                        "score": round(len(entities) / max(1, len(KEYS[role])), 2),
                        "current": current.get(role) == device.id,
                    }
                )
            found.sort(key=lambda c: (not c["current"], -c["score"], c["name"]))
            if found or role in current:
                out[role] = found[:6]
        return out

    @callback
    def unlink(self, role: str) -> int:
        """Remove the device subentries of a role (the setup assistant's "none")."""
        subs = [s for s in self.links() if s.data.get(CONF_ROLE) == role]
        for sub in subs:
            self.hass.config_entries.async_remove_subentry(self.entry, sub.subentry_id)
        return len(subs)

    def outdoor(self) -> dict[str, str]:
        """Return outdoor temperature, humidity and pressure: weather station, else found ones."""
        station = self.first("weather_station")
        out = {
            k: station["entities"][k]
            for k in OUTDOOR_CLASSES
            if station and k in station["entities"]
        }
        registry = er.async_get(self.hass)
        devices = dr.async_get(self.hass)
        areas = ar.async_get(self.hass)

        def score(state: Any) -> int:
            entry = registry.async_get(state.entity_id)
            if entry is not None and (entry.hidden_by or entry.entity_category):
                return -1
            area_id = entry.area_id if entry else None
            if entry is not None and not area_id and entry.device_id:
                device = devices.async_get(entry.device_id)
                area_id = device.area_id if device else None
            area = areas.async_get_area(area_id) if area_id else None
            if area and _OUTDOOR_AREA.search(f"{area.name} {area.id}"):
                return 2
            name = f"{state.entity_id} {state.attributes.get('friendly_name', '')}"
            return 1 if _OUTDOOR_NAME.search(name) else 0

        states = self.hass.states.async_all("sensor")
        for key, classes in OUTDOOR_CLASSES.items():
            if key in out:
                continue
            best: tuple[int, str] | None = None
            for state in states:
                if state.attributes.get("device_class") not in classes:
                    continue
                name = f"{state.entity_id} {state.attributes.get('friendly_name', '')}"
                if _OUTDOOR_NOT.search(name):
                    continue
                try:
                    float(state.state)
                except ValueError:
                    continue
                points = score(state)
                if points > 0 and (best is None or points > best[0]):
                    best = (points, state.entity_id)
            if best:
                out[key] = best[1]
        return out

    def info(self, migrated: bool, steps: list[int] | None = None) -> dict[str, Any]:
        """Return the catalog for the dashboard (steps: the takeovers done after the first)."""
        return {
            "migrated": migrated,
            "steps": [1, *(steps or [])] if migrated else list(steps or []),
            "devices": self.devices(),
            "outdoor": self.outdoor(),
            "roles": ROLE_LABELS,
        }
