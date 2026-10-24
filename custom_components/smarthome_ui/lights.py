"""Motion light: suggestions per room and the central day and night brightness.

Rooms (areas) with a motion or presence sensor and lights but no automation that switches those
lights by that sensor are suggested. Accepting one creates a Home Assistant automation from the
blueprint ``motion_light.yaml`` (light on with motion, off after a delay without). The brightness
comes from Core's entities ``number.lights_brightness_day`` / ``number.lights_brightness_night``
and the night from ``time.lights_night_start`` / ``time.lights_night_end`` – one place for every
room.
"""

from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import DOMAIN, SIGNAL_DATA
from .store import CoreStore

SIGNAL_LIGHTS = f"{DOMAIN}_lights"
BLUEPRINT = f"{DOMAIN}/motion_light.yaml"
MOTION_CLASSES = ("motion", "occupancy", "presence")
DEFAULTS: dict[str, Any] = {
    "day": 100,
    "night": 20,
    "night_start": "22:00",
    "night_end": "06:30",
}
# the entities the blueprint reads (so a suggestion can fill them in)
ENTITIES = {
    "day_brightness": "number.lights_brightness_day",
    "night_brightness": "number.lights_brightness_night",
    "night_start": "time.lights_night_start",
    "night_end": "time.lights_night_end",
}


class LightSettings:
    """The central brightness and night times (Core entities, the dashboard sets them too)."""

    def __init__(self, hass: HomeAssistant, store: CoreStore) -> None:
        """Initialize."""
        self.hass = hass
        self.store = store

    def settings(self) -> dict[str, Any]:
        """Return the settings with defaults."""
        own = self.store.data.get("lights") or {}
        return {k: own.get(k, v) for k, v in DEFAULTS.items()}

    @callback
    def set(self, key: str, value: Any) -> None:
        """Change one setting."""
        if key not in DEFAULTS:
            raise ValueError(key)
        if key in ("day", "night"):
            value = max(1, min(100, int(value)))
        else:
            hour, _, minute = str(value).partition(":")
            value = f"{int(hour) % 24:02d}:{int(minute[:2] or 0) % 60:02d}"
        self.store.data["lights"] = {**(self.store.data.get("lights") or {}), key: value}
        self.store.save(SIGNAL_DATA)
        async_dispatcher_send(self.hass, SIGNAL_LIGHTS)

    def info(self) -> dict[str, Any]:
        """Return the settings and the entities for the dashboard."""
        return {**self.settings(), "entities": ENTITIES, "blueprint": BLUEPRINT}


def _automations(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the automations with their configuration (entity, name, raw config)."""
    component = hass.data.get("automation")
    out = []
    for ent in getattr(component, "entities", []) or []:
        raw = getattr(ent, "raw_config", None)
        if not isinstance(raw, dict):
            continue
        out.append({"entity_id": ent.entity_id, "name": ent.name, "id": raw.get("id"), "raw": raw})
    return out


def _mentions(raw: Any, ids: set[str]) -> set[str]:
    """Return which of the entity IDs appear anywhere in an automation's configuration."""
    text = str(raw)
    return {i for i in ids if i in text}


def suggest(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return per room: motion sensors, lights and the automation that already covers them."""
    ents = er.async_get(hass)
    devs = dr.async_get(hass)
    areas = ar.async_get(hass)
    rooms: dict[str, dict[str, list[str]]] = {}
    for e in ents.entities.values():
        if (
            e.disabled_by
            or e.hidden_by
            or e.platform == DOMAIN
            or e.domain not in ("binary_sensor", "light")
        ):
            continue
        area = e.area_id or (
            devs.async_get(e.device_id).area_id
            if e.device_id and devs.async_get(e.device_id)
            else None
        )
        if not area:
            continue
        if e.domain == "binary_sensor":
            state = hass.states.get(e.entity_id)
            dc = (
                e.device_class
                or e.original_device_class
                or (state.attributes.get("device_class") if state else None)
            )
            if dc not in MOTION_CLASSES:
                continue
            rooms.setdefault(area, {"motion": [], "lights": []})["motion"].append(e.entity_id)
        else:
            rooms.setdefault(area, {"motion": [], "lights": []})["lights"].append(e.entity_id)
    autos = _automations(hass)
    out = []
    for area_id, r in rooms.items():
        if not r["motion"] or not r["lights"]:
            continue
        area = areas.async_get_area(area_id)
        motion, lights = set(r["motion"]), set(r["lights"])
        covered = None
        for a in autos:
            if _mentions(a["raw"], motion) and _mentions(a["raw"], lights):
                bp = (a["raw"].get("use_blueprint") or {}).get("path")
                covered = {
                    "entity_id": a["entity_id"],
                    "name": a["name"],
                    "id": a["id"],
                    "ours": bp == BLUEPRINT,
                    "input": (a["raw"].get("use_blueprint") or {}).get("input")
                    if bp == BLUEPRINT
                    else None,
                }
                break
        out.append(
            {
                "area_id": area_id,
                "area": area.name if area else area_id,
                "floor_id": getattr(area, "floor_id", None),
                "motion": sorted(motion),
                "lights": sorted(lights),
                "automation": covered,
            }
        )
    out.sort(key=lambda x: (x["automation"] is not None, x["area"].lower()))
    return out
