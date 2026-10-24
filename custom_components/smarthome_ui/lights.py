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

    def ignored(self) -> list[str]:
        """Return the rooms whose suggestion was hidden."""
        return list(self.store.data.get("lights_ignored") or [])

    @callback
    def ignore(self, area_id: str, hide: bool = True) -> None:
        """Hide (or show again) the suggestion for a room."""
        rooms = set(self.ignored())
        if hide:
            rooms.add(area_id)
        else:
            rooms.discard(area_id)
        self.store.data["lights_ignored"] = sorted(rooms)
        self.store.save(SIGNAL_DATA)

    def info(self) -> dict[str, Any]:
        """Return the settings and the entities for the dashboard."""
        return {**self.settings(), "entities": ENTITIES, "blueprint": BLUEPRINT}


def _automations(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return the automations: entity, name, config, blueprint (with its inputs) and references."""
    component = hass.data.get("automation")
    out = []
    for ent in getattr(component, "entities", []) or []:
        raw = getattr(ent, "raw_config", None)
        if not isinstance(raw, dict):
            continue
        # a blueprint automation keeps its use_blueprint block apart from the expanded config
        source = getattr(ent, "_blueprint_inputs", None) or {}
        bp = source.get("use_blueprint") or raw.get("use_blueprint") or {}
        refs: set[str] = set()
        for attr in ("referenced_entities", "referenced_devices", "referenced_areas"):
            try:
                refs |= set(getattr(ent, attr, None) or ())
            except Exception:  # noqa: BLE001 - a broken automation still counts by its text
                pass
        out.append(
            {
                "entity_id": ent.entity_id,
                "name": ent.name,
                "id": raw.get("id") or source.get("id"),
                "raw": raw,
                "blueprint": getattr(ent, "referenced_blueprint", None) or bp.get("path"),
                "input": bp.get("input"),
                "refs": refs,
            }
        )
    return out


def _mentions(auto: dict[str, Any], ids: set[str]) -> bool:
    """Return True if one of the IDs is referenced by the automation (or appears in its config)."""
    if auto["refs"] & ids:
        return True
    text = str(auto["raw"])
    return any(i in text for i in ids)


def suggest(hass: HomeAssistant, ignored: Any = None) -> list[dict[str, Any]]:
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
            r = rooms.setdefault(
                area, {"motion": [], "lights": [], "motion_dev": [], "light_dev": []}
            )
            r["motion"].append(e.entity_id)
            if e.device_id:
                r["motion_dev"].append(e.device_id)
        else:
            r = rooms.setdefault(
                area, {"motion": [], "lights": [], "motion_dev": [], "light_dev": []}
            )
            r["lights"].append(e.entity_id)
            if e.device_id:
                r["light_dev"].append(e.device_id)
    autos = _automations(hass)
    ignored = set(ignored or ())
    # light groups and scenes holding lights (an automation may switch the light through them)
    holders = [
        (st.entity_id, set(st.attributes.get("entity_id") or []))
        for st in [*hass.states.async_all("light"), *hass.states.async_all("scene")]
        if isinstance(st.attributes.get("entity_id"), list)
    ]
    out = []
    for area_id, r in rooms.items():
        if not r["motion"] or not r["lights"]:
            continue
        area = areas.async_get_area(area_id)
        motion, lights = set(r["motion"]), set(r["lights"])
        # covered: a sensor (entity or device) and a light (entity, device, area, or a light group
        # or scene with a light of the room) are referenced by the same automation
        motion_refs = motion | set(r["motion_dev"])
        light_refs = (
            lights
            | set(r["light_dev"])
            | {area_id}
            | {h for h, members in holders if members & lights}
        )
        covered = None
        for a in autos:
            if _mentions(a, motion_refs) and _mentions(a, light_refs):
                ours = a["blueprint"] == BLUEPRINT
                covered = {
                    "entity_id": a["entity_id"],
                    "name": a["name"],
                    "id": a["id"],
                    "ours": ours,
                    "input": a["input"] if ours else None,
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
                "ignored": area_id in ignored,
            }
        )
    out.sort(key=lambda x: (x["automation"] is not None or x["ignored"], x["area"].lower()))
    return out


# ---------------------------------------------------------------- label and category

LABEL = "Smart-Core"
# category (Settings → Automations) per Core blueprint: by application
CATEGORIES = {
    f"{DOMAIN}/motion_light.yaml": ("Licht", "mdi:lightbulb-auto-outline"),
    f"{DOMAIN}/vacuum_when_leaving.yaml": ("Saugen", "mdi:robot-vacuum"),
}


@callback
def tag_automations(hass: HomeAssistant) -> int:
    """Give automations from Core's blueprints the label "Smart-Core" and a category.

    A category the user chose stays; the label is only added. Returns how many were changed.
    """
    from homeassistant.helpers import category_registry as cr, label_registry as lr

    autos = [a for a in _automations(hass) if str(a["blueprint"] or "").startswith(f"{DOMAIN}/")]
    if not autos:
        return 0
    labels = lr.async_get(hass)
    label = labels.async_get_label_by_name(LABEL) or labels.async_create(
        LABEL, color="amber", icon="mdi:creation", description="Angelegt von Smarthome UI Core"
    )
    cats = cr.async_get(hass)
    ents = er.async_get(hass)
    changed = 0
    for a in autos:
        entry = ents.async_get(a["entity_id"])
        if entry is None:
            continue
        update: dict[str, Any] = {}
        if label.label_id not in entry.labels:
            update["labels"] = {*entry.labels, label.label_id}
        path = a["blueprint"]
        if path in CATEGORIES and "automation" not in (entry.categories or {}):
            name, icon = CATEGORIES[path]
            cat = next(
                (c for c in cats.async_list_categories(scope="automation") if c.name == name),
                None,
            ) or cats.async_create(name=name, scope="automation", icon=icon)
            update["categories"] = {**(entry.categories or {}), "automation": cat.category_id}
        if update:
            ents.async_update_entity(a["entity_id"], **update)
            changed += 1
    return changed
