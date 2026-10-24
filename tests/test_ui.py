"""The dashboard card: fetched from GitHub, installed, served, offered as update."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.smarthome_ui.ui import API, CARD, UiManager, notes_since
from homeassistant.core import HomeAssistant

REAL_GET = UiManager._get
CHANGELOG = """# Änderungen

## 1.282.0
- Neu: Kino

## 1.281.0
- Neu: Core liefert die Karte

## 1.280.0
- Alt
"""


def _card(version: str) -> str:
    return f'const CARD_VERSION = "{version}";\nclass X {{}}\n'


def _mock(aioclient_mock: AiohttpClientMocker, version: str, ref: str = "main") -> None:
    aioclient_mock.clear_requests()
    aioclient_mock.get(API + CARD, params={"ref": ref}, text=_card(version))
    aioclient_mock.get(API + "CHANGELOG.md", params={"ref": ref}, text=CHANGELOG)


async def test_check_install_update(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, hass_ws_client
) -> None:
    await hass.async_add_executor_job(
        Path(hass.config.path(".smarthome_ui_token")).write_text, "geheim\n"
    )
    ui = entry.runtime_data.ui
    with patch.object(UiManager, "_get", REAL_GET):
        _mock(aioclient_mock, "1.281.0")
        # first start: checks and installs by itself
        await ui.async_check_first()
        await hass.async_block_till_done()
        assert ui.data["installed"] == "1.281.0"
        assert aioclient_mock.mock_calls[0][3]["Authorization"] == "Bearer geheim"
        card = Path(hass.config.path("smarthome_ui", "smarthome-dashboard-card.js"))
        assert "1.281.0" in await hass.async_add_executor_job(card.read_text)
        assert ui.info()["url"] == "/smarthome_ui_ui/smarthome-dashboard-card.js?v=1.281.0"
        assert hass.states.get("update.system_dashboard").state == "off"

        # a new version: offered as update with what is new
        _mock(aioclient_mock, "1.282.0")
        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": "smarthome_ui/ui/check"})
        res = (await client.receive_json())["result"]
        assert res["latest"] == "1.282.0" and res["installed"] == "1.281.0"
        assert "Kino" in res["notes"] and "Core liefert" not in res["notes"]
        await hass.async_block_till_done()
        state = hass.states.get("update.system_dashboard")
        assert state.state == "on" and state.attributes["latest_version"] == "1.282.0"

        await hass.services.async_call(
            "update", "install", {"entity_id": "update.system_dashboard"}, blocking=True
        )
        assert ui.data["installed"] == "1.282.0"
        assert ui.info()["url"].endswith("?v=1.282.0")
        assert hass.states.get("update.system_dashboard").state == "off"

        await client.send_json({"id": 2, "type": "smarthome_ui/get"})
        assert (await client.receive_json())["result"]["ui"]["installed"] == "1.282.0"


async def test_errors(
    hass: HomeAssistant, entry: MockConfigEntry, aioclient_mock: AiohttpClientMocker, hass_ws_client
) -> None:
    ui = entry.runtime_data.ui
    with patch.object(UiManager, "_get", REAL_GET):
        aioclient_mock.get(API + CARD, params={"ref": "main"}, status=404)
        await ui.async_check_first()
        assert "nicht gefunden" in ui.data["error"] and not ui.data.get("installed")
        client = await hass_ws_client(hass)
        await client.send_json({"id": 1, "type": "smarthome_ui/ui/install"})
        res = await client.receive_json()
        assert res["success"] is False and res["error"]["code"] == "ui_failed"


async def test_options_branch(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"ui_ref": " dev ", "ui_token": ""}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    entry = hass.config_entries.async_get_entry(entry.entry_id)
    assert entry.options == {"ui_ref": "dev", "ui_token": ""}
    assert entry.runtime_data.ui.ref == "dev"


def test_notes_since() -> None:
    assert notes_since(CHANGELOG, "1.280.0", "1.282.0").startswith("## 1.282.0")
    assert "1.280.0" not in notes_since(CHANGELOG, "1.280.0", "1.282.0")
    assert notes_since(CHANGELOG, "1.282.0", "1.282.0") == ""
    assert "1.282.0" not in notes_since(CHANGELOG, "1.280.0", "1.281.0")
