"""Shared test fixtures."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from custom_components.smarthome_ui.const import DOMAIN
from custom_components.smarthome_ui.ui import UiError, UiManager
from homeassistant.core import HomeAssistant


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading custom integrations in all tests."""
    return


@pytest.fixture(autouse=True)
def github_offline():
    """GitHub is not reached in tests (the dashboard card); test_ui replaces this."""

    async def offline(self, path: str) -> str:
        raise UiError("offline")

    with patch.object(UiManager, "_get", offline):
        yield


@pytest.fixture
def notify_calls(hass: HomeAssistant):
    """Collect calls to notify.mobile_app_phone."""
    return async_mock_service(hass, "notify", "mobile_app_phone")


@pytest.fixture
async def entry(hass: HomeAssistant, notify_calls) -> MockConfigEntry:
    """Set up the integration."""
    entry = MockConfigEntry(domain=DOMAIN, data={}, title="Smarthome UI Core")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry
