"""Setup assistant: robot mowers, robot vacuums, household appliances and doorbells.

These are subentries of their own (with own settings and entities). The assistant shows the
devices found in Home Assistant and adds the chosen ones – the same as "… hinzufügen" in the
integration, without the settings (they keep their defaults and can be changed there later).
"""

from __future__ import annotations

import re
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .appliance import CONF_KIND, KIND_OPTIONS, LABELS, SUBENTRY_APPLIANCE, detect_kind
from .doorbell import SUBENTRY_DOORBELL
from .mower import CONF_DEVICE, SUBENTRY_MOWER
from .vacuum import SUBENTRY_VACUUM

TYPES = (SUBENTRY_MOWER, SUBENTRY_VACUUM, SUBENTRY_APPLIANCE, SUBENTRY_DOORBELL)
DEFAULT_NAMES = {
    SUBENTRY_MOWER: "Mähroboter",
    SUBENTRY_VACUUM: "Saugroboter",
    SUBENTRY_APPLIANCE: "Haushaltsgerät",
    SUBENTRY_DOORBELL: "Klingel",
}
_BELL = re.compile(r"(doorbell|klingel|ring)", re.IGNORECASE)
_NOT_BELL = re.compile(r"(chime|gong|volume|lautst|repeat)", re.IGNORECASE)


def _added(entry: ConfigEntry) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {t: set() for t in TYPES}
    for sub in entry.subentries.values():
        if sub.subentry_type in out and sub.data.get(CONF_DEVICE):
            out[sub.subentry_type].add(sub.data[CONF_DEVICE])
    return out


def suggest_devices(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, list[dict[str, Any]]]:
    """Return the devices that could be added, per type (added ones marked)."""
    devices = dr.async_get(hass)
    entities = [
        e
        for e in er.async_get(hass).entities.values()
        if e.device_id and not e.disabled_by and e.platform != "smarthome_ui"
    ]
    by_device: dict[str, list[er.RegistryEntry]] = {}
    for e in entities:
        by_device.setdefault(e.device_id, []).append(e)
    added = _added(entry)
    out: dict[str, list[dict[str, Any]]] = {t: [] for t in TYPES}

    def item(sub_type: str, device: dr.DeviceEntry, **extra: Any) -> dict[str, Any]:
        return {
            "device_id": device.id,
            "name": device.name_by_user or device.name or DEFAULT_NAMES[sub_type],
            "manufacturer": device.manufacturer,
            "model": device.model,
            "added": device.id in added[sub_type],
            **extra,
        }

    for device_id, ents in by_device.items():
        device = devices.async_get(device_id)
        if device is None:
            continue
        domains = {e.domain for e in ents}
        if "lawn_mower" in domains:
            out[SUBENTRY_MOWER].append(item(SUBENTRY_MOWER, device))
        if "vacuum" in domains:
            out[SUBENTRY_VACUUM].append(item(SUBENTRY_VACUUM, device))
        bells = [
            e
            for e in ents
            if e.domain in ("event", "binary_sensor")
            and (
                (e.device_class or e.original_device_class) == "doorbell"
                or _BELL.search(f"{e.entity_id} {e.original_name or ''}")
            )
            and not _NOT_BELL.search(f"{e.entity_id} {e.original_name or ''}")
        ]
        if bells:
            camera = next((e.entity_id for e in ents if e.domain == "camera"), None)
            lock = next((e.entity_id for e in ents if e.domain == "lock"), None)
            out[SUBENTRY_DOORBELL].append(item(SUBENTRY_DOORBELL, device, camera=camera, lock=lock))
        kind = detect_kind(f"{device.name_by_user or ''} {device.name or ''} {device.model or ''}")
        if kind != "other" and domains & {"sensor", "binary_sensor", "select", "switch"}:
            out[SUBENTRY_APPLIANCE].append(
                item(SUBENTRY_APPLIANCE, device, kind=kind, kind_label=LABELS[kind])
            )
    for items in out.values():
        items.sort(key=lambda c: (not c["added"], c["name"]))
    return out


@callback
def add_device(
    hass: HomeAssistant,
    entry: ConfigEntry,
    kind: str,
    device_id: str,
    name: str | None = None,
    appliance_kind: str | None = None,
    camera: str | None = None,
    lock: str | None = None,
) -> str | None:
    """Add a mower / vacuum / appliance / doorbell (None if it is there already)."""
    if kind not in TYPES:
        raise ValueError(kind)
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        raise ValueError(device_id)
    if device_id in _added(entry)[kind]:
        return None
    data: dict[str, Any] = {CONF_DEVICE: device_id}
    if kind == SUBENTRY_APPLIANCE:
        data[CONF_KIND] = appliance_kind if appliance_kind in KIND_OPTIONS else "auto"
    if kind == SUBENTRY_DOORBELL:
        data["camera"] = camera or None
        data["lock"] = lock or None
    title = (name or "").strip() or device.name_by_user or device.name or DEFAULT_NAMES[kind]
    sub = ConfigSubentry(data=data, subentry_type=kind, title=title, unique_id=None)
    hass.config_entries.async_add_subentry(entry, sub)
    return sub.subentry_id
