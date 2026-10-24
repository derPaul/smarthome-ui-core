"""Diagnostics: what Core knows, names and targets made unrecognisable."""

from __future__ import annotations

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)

from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component


async def test_diagnostics(hass: HomeAssistant, entry: MockConfigEntry, hass_client) -> None:
    assert await async_setup_component(hass, "diagnostics", {})
    store = entry.runtime_data.store
    store.data["notify"]["targets"] = ["mobile_app_pauls_iphone"]
    store.data["alarm"] = {"configured": True, "guests_on": True, "guests_manual": True}
    store.data["data"]["done_log"] = [{"k": "a", "who": "Paul"}, {"k": "b", "who": "Anna"}]
    diag = await get_diagnostics_for_config_entry(hass, hass_client, entry)
    assert diag["version"]
    assert diag["alarm_internal"]["guests_manual"] is True
    assert diag["settings"]["notify"]["targets"] == "**REDACTED**"
    assert diag["state"]["data"]["done_log"] == 2  # only the count
    assert "catalog" in diag["state"] and "lists" in diag["state"]
    assert "Paul" not in str(diag) and "pauls_iphone" not in str(diag)
