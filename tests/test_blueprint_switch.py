"""Blueprint "Schalter-Tasten": short, long and held presses per button."""

from __future__ import annotations

from datetime import timedelta

from pytest_homeassistant_custom_component.common import MockConfigEntry, async_mock_service

from homeassistant.components.blueprint import models
from homeassistant.components.blueprint.schemas import BLUEPRINT_SCHEMA
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from homeassistant.util.yaml import load_yaml

from .test_blueprints_heating import FOLDER, _automation


def test_switch_blueprint_valid() -> None:
    bp = models.Blueprint(
        load_yaml(FOLDER / "switch_buttons.yaml"),
        expected_domain="automation",
        schema=BLUEPRINT_SCHEMA,
    )
    assert bp.metadata["source_url"].endswith("switch_buttons.yaml")


async def test_switch_buttons(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    on = "event.dimmer_button_1"
    up = "event.dimmer_button_2"
    for eid in (on, up):
        hass.states.async_set(eid, "unknown", {"event_types": ["short_release", "long_press"]})
    toggle = async_mock_service(hass, "light", "toggle")
    turn_on = async_mock_service(hass, "light", "turn_on")
    await _automation(
        hass,
        "switch_buttons",
        {
            "button_1": [on],
            "short_1": [{"action": "light.toggle", "target": {"entity_id": "light.a"}}],
            "long_1": [{"action": "light.turn_on", "target": {"entity_id": "light.b"}}],
            "button_2": [up],
            "hold_2": [
                {
                    "action": "light.turn_on",
                    "target": {"entity_id": "light.a"},
                    "data": {"brightness_step_pct": 10},
                }
            ],
        },
    )

    async def press(eid: str, kind: str, n: int) -> None:
        stamp = (dt_util.utcnow() + timedelta(milliseconds=n)).isoformat()
        hass.states.async_set(eid, stamp, {"event_type": kind})
        await hass.async_block_till_done()

    await press(on, "initial_press", 1)  # ignored
    await press(on, "short_release", 2)
    assert [c.data["entity_id"] for c in toggle] == [["light.a"]]
    assert not turn_on
    await press(on, "long_press", 3)
    assert [c.data["entity_id"] for c in turn_on] == [["light.b"]]
    await press(up, "repeat", 4)
    await press(up, "repeat", 5)
    assert [c.data.get("brightness_step_pct") for c in turn_on[1:]] == [10, 10]
    await press(up, "short_release", 6)  # no action for short on button 2
    assert len(toggle) == 1 and len(turn_on) == 3
    # restored old event (restart): nothing
    hass.states.async_set(on, "unavailable", {})
    hass.states.async_set(on, "2026-01-01T10:00:00+00:00", {"event_type": "short_release"})
    await hass.async_block_till_done()
    assert len(toggle) == 1
