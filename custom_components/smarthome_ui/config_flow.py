"""Config flow for Smarthome UI Core; robot mowers and vacuums are added as subentries."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.selector import (
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TimeSelector,
)

from .const import DOMAIN
from .mower import CONF_DEVICE, SUBENTRY_MOWER
from .planner import DEFAULTS, KIND_MOWER, KIND_VACUUM
from .vacuum import SUBENTRY_VACUUM


class SmarthomeUiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Smarthome UI Core once."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm adding the integration."""
        if user_input is not None:
            return self.async_create_entry(title="Smarthome UI Core", data={})
        return self.async_show_form(step_id="user")

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries: robot mowers and robot vacuums."""
        return {SUBENTRY_MOWER: MowerSubentryFlow, SUBENTRY_VACUUM: VacuumSubentryFlow}


class MowerSubentryFlow(ConfigSubentryFlow):
    """Add or change a robot mower: pick its device, optionally give it a name."""

    _domain = "lawn_mower"
    _type = SUBENTRY_MOWER
    _default = "Mähroboter"
    _kind = KIND_MOWER

    def _planner_schema(self, current: dict[str, Any]) -> dict[Any, Any]:
        """Settings of the robot planner (when it may start on its own)."""
        cur = {**DEFAULTS[self._kind], **{k: v for k, v in current.items() if v is not None}}

        def opt(key: str) -> vol.Optional:
            value = cur.get(key)
            return vol.Optional(key, default=value) if value is not None else vol.Optional(key)

        fields: dict[Any, Any] = {
            opt("auto_from"): TimeSelector(),
            opt("auto_to"): TimeSelector(),
            opt("cond_quiet"): BooleanSelector(),
            opt("cond_away"): BooleanSelector(),
        }
        if self._kind == KIND_MOWER:
            fields[opt("cond_rain")] = BooleanSelector()
        fields[opt("cond_solar")] = BooleanSelector()
        fields[
            vol.Optional("solar_entity", description={"suggested_value": cur.get("solar_entity")})
        ] = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
        fields[opt("solar_min")] = NumberSelector(
            NumberSelectorConfig(
                min=0, max=20000, step=50, unit_of_measurement="W", mode=NumberSelectorMode.BOX
            )
        )
        if self._kind == KIND_VACUUM:
            fields[opt("mop_every")] = NumberSelector(
                NumberSelectorConfig(
                    min=0, max=60, step=1, unit_of_measurement="d", mode=NumberSelectorMode.BOX
                )
            )
        fields[opt("announce")] = NumberSelector(
            NumberSelectorConfig(
                min=0, max=60, step=1, unit_of_measurement="min", mode=NumberSelectorMode.BOX
            )
        )
        return fields

    def _schema(self, device: str | None = None, name: str | None = None) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=device or vol.UNDEFINED): DeviceSelector(
                    DeviceSelectorConfig(entity=[EntityFilterSelectorConfig(domain=self._domain)])
                ),
                vol.Optional("name", description={"suggested_value": name}): TextSelector(),
            }
        )

    def _title(self, user_input: dict[str, Any]) -> str:
        if name := (user_input.get("name") or "").strip():
            return name
        device = dr.async_get(self.hass).async_get(user_input[CONF_DEVICE])
        return (device.name_by_user or device.name or self._default) if device else self._default

    def _taken(self, device_id: str, own: str | None = None) -> bool:
        return any(
            sub.subentry_type == self._type
            and sub.data.get(CONF_DEVICE) == device_id
            and sub.subentry_id != own
            for sub in self._get_entry().subentries.values()
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a device."""
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE]):
                return self.async_abort(reason="already_configured")
            return self.async_create_entry(
                title=self._title(user_input), data={CONF_DEVICE: user_input[CONF_DEVICE]}
            )
        return self.async_show_form(step_id="user", data_schema=self._schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change device or name."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE], subentry.subentry_id):
                return self.async_abort(reason="already_configured")
            data = {k: v for k, v in user_input.items() if k != "name"}
            for key in ("auto_from", "auto_to"):
                if data.get(key):
                    data[key] = str(data[key])[:5]
            for key in ("solar_min", "announce", "mop_every"):
                if data.get(key) is not None:
                    data[key] = int(data[key])
            return self.async_update_and_abort(
                self._get_entry(), subentry, title=self._title(user_input), data=data
            )
        schema = self._schema(subentry.data.get(CONF_DEVICE), subentry.title).extend(
            self._planner_schema(dict(subentry.data))
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema)


class VacuumSubentryFlow(MowerSubentryFlow):
    """Add or change a robot vacuum."""

    _domain = "vacuum"
    _type = SUBENTRY_VACUUM
    _default = "Saugroboter"
    _kind = KIND_VACUUM
