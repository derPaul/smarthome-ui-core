"""Diagnostics ("Diagnose herunterladen" at the integration): what Core knows and why it acted.

Everything the dashboard gets (catalog, lists, alarm, last night, house technology, system …), the
alarm's inner state (guests by hand, last reason), the settings handed over by the dashboard and the
last notifications – names, people and notification targets made unrecognisable. The household data
(done records, logs) only as counts.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.loader import async_get_integration

from .const import DOMAIN

TO_REDACT = {
    "name",
    "names",
    "nick",
    "full",
    "who",
    "by",
    "title",
    "friendly_name",
    "user",
    "users",
    "person",
    "persons",
    "targets",
    "ssids",
    "external_ip",
    "wan_ip",
    "latitude",
    "longitude",
    "address",
    "message",
}
# settings handed over by the dashboard (store keys)
SETTINGS = (
    "notify",
    "security",
    "technik",
    "system",
    "mailbox",
    "presence_sim",
    "waste",
    "cleaning",
    "task_defs",
    "catalog_migrated",
    "catalog_steps",
    "onboarding",
)


def _counts(value: Any) -> Any:
    """Return the size of lists and dicts (household data without the records themselves)."""
    if isinstance(value, dict):
        return {k: len(v) if isinstance(v, (list, dict)) else v for k, v in value.items()}
    return len(value) if isinstance(value, list) else value


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return the diagnostics of the integration."""
    from . import _state  # the dashboard's view (catalog, lists, alarm, last night …)

    runtime = entry.runtime_data
    store = runtime.store.data
    state = _state(runtime)
    state["data"] = _counts(state.get("data") or {})
    alarm = store.get("alarm") or {}
    integration = await async_get_integration(hass, DOMAIN)
    out = {
        "version": str(integration.version),
        "home_assistant": HA_VERSION,
        "subentries": [
            {"type": s.subentry_type, "title": s.title, "data": dict(s.data)}
            for s in entry.subentries.values()
        ],
        "alarm_internal": {
            key: alarm.get(key)
            for key in (
                "configured",
                "enabled",
                "panel",
                "guests_on",
                "guests_manual",
                "reason",
                "nightwalk_until",
            )
        },
        "settings": {key: store.get(key) for key in SETTINGS},
        # which sensor triggers in which mode, silent with guests, night walk (to explain an alarm)
        "alarm_settings": {
            "rules": alarm.get("rules"),
            "guests": alarm.get("guests"),
            "nightwalk": alarm.get("nightwalk"),
        },
        "recent_notifications": [
            {k: v for k, v in n.items() if k in ("src", "t", "tag", "urgent", "title")}
            for n in (store.get("recent") or [])[-30:]
            if isinstance(n, dict)
        ],
        "state": state,
    }
    return async_redact_data(out, TO_REDACT)
