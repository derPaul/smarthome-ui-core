"""Config flow for Smarthome UI Core; robots and household appliances are added as subentries."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    ConfigSubentryFlow,
    OptionsFlow,
    SubentryFlowResult,
)
from homeassistant.core import callback
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    DeviceSelector,
    DeviceSelectorConfig,
    EntityFilterSelectorConfig,
    EntitySelector,
    EntitySelectorConfig,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
    TimeSelector,
)

from .appliance import (
    CONF_KIND,
    DEFAULTS as APPLIANCE_DEFAULTS,
    KIND_OPTIONS,
    SUBENTRY_APPLIANCE,
    detect_kind,
)
from .catalog import (
    CONF_CAPACITY,
    CONF_ENTITIES,
    CONF_ROLE,
    KEYS,
    ROLE_LABELS,
    ROLES,
    SUBENTRY_DEVICE,
    Catalog,
)
from .const import DOMAIN
from .doorbell import SUBENTRY_DOORBELL
from .maintenance import (
    CONF_AREA,
    CONF_KIND as PART_KIND,
    CONF_SINCE,
    KIND_OPTIONS as PART_KINDS,
    KINDS as PART_SPECS,
    SUBENTRY_PART,
    default_months,
)
from .mower import CONF_DEVICE, SUBENTRY_MOWER
from .planner import DEFAULTS, KIND_MOWER, KIND_VACUUM
from .store import now_ms
from .vacuum import SUBENTRY_VACUUM


class SmarthomeUiConfigFlow(ConfigFlow, domain=DOMAIN):
    """Add Smarthome UI Core once."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Confirm adding the integration."""
        if user_input is not None:
            return self.async_create_entry(title="Smarthome UI Core", data={})
        return self.async_show_form(step_id="user")

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options: where the dashboard card comes from."""
        return SmarthomeUiOptionsFlow()

    @classmethod
    @callback
    def async_get_supported_subentry_types(
        cls, config_entry: ConfigEntry
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries: robot mowers, robot vacuums and household appliances."""
        return {
            SUBENTRY_MOWER: MowerSubentryFlow,
            SUBENTRY_VACUUM: VacuumSubentryFlow,
            SUBENTRY_APPLIANCE: ApplianceSubentryFlow,
            SUBENTRY_PART: PartSubentryFlow,
            SUBENTRY_DOORBELL: DoorbellSubentryFlow,
            SUBENTRY_DEVICE: DeviceSubentryFlow,
        }


class SmarthomeUiOptionsFlow(OptionsFlow):
    """Branch and token for fetching the dashboard card from GitHub."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the options (saving reloads Core, which then checks again)."""
        if user_input is not None:
            return self.async_create_entry(
                data={
                    "ui_ref": (user_input.get("ui_ref") or "").strip() or "main",
                    "ui_token": (user_input.get("ui_token") or "").strip(),
                }
            )
        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required("ui_ref", default=options.get("ui_ref") or "main"): str,
                    vol.Optional(
                        "ui_token",
                        description={"suggested_value": options.get("ui_token") or ""},
                    ): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
                }
            ),
        )


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


class ApplianceSubentryFlow(MowerSubentryFlow):
    """Add or change a household appliance (dishwasher, washer, dryer, fridge …)."""

    _type = SUBENTRY_APPLIANCE
    _default = "Haushaltsgerät"

    def _schema(self, device: str | None = None, name: str | None = None) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=device or vol.UNDEFINED): DeviceSelector(),
                vol.Optional("name", description={"suggested_value": name}): TextSelector(),
            }
        )

    def _kind_field(self, current: str) -> dict[Any, Any]:
        return {
            vol.Required(CONF_KIND, default=current): SelectSelector(
                SelectSelectorConfig(
                    options=KIND_OPTIONS,
                    translation_key="appliance_kind",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        }

    def _resolved_kind(self, data: dict[str, Any]) -> str:
        kind = data.get(CONF_KIND) or "auto"
        if kind != "auto":
            return kind
        device = dr.async_get(self.hass).async_get(data.get(CONF_DEVICE) or "")
        if device is None:
            return "other"
        return detect_kind(f"{device.name_by_user or ''} {device.name or ''} {device.model or ''}")

    def _options_schema(self, kind: str, current: dict[str, Any]) -> dict[Any, Any]:
        cur = {
            **APPLIANCE_DEFAULTS,
            **{k: v for k, v in current.items() if v is not None},
        }

        def number(key: str, low: float, high: float, unit: str, step: float = 1) -> tuple:
            return (
                vol.Optional(key, default=cur[key]),
                NumberSelector(
                    NumberSelectorConfig(
                        min=low,
                        max=high,
                        step=step,
                        unit_of_measurement=unit,
                        mode=NumberSelectorMode.BOX,
                    )
                ),
            )

        fields: dict[Any, Any] = {}
        if kind == "fridge":
            for key, low, high, unit in (
                ("door_minutes", 1, 60, "min"),
                ("max_temp", 2, 15, "°C"),
                ("freezer_max_temp", -30, 0, "°C"),
            ):
                field, selector = number(key, low, high, unit)
                fields[field] = selector
            return fields
        field, selector = number("reminder", 0, 600, "min")
        fields[field] = selector
        fields[
            vol.Optional("solar_entity", description={"suggested_value": cur.get("solar_entity")})
        ] = EntitySelector(EntitySelectorConfig(domain="sensor", device_class="power"))
        field, selector = number("solar_min", 0, 20000, "W", 50)
        fields[field] = selector
        return fields

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add an appliance."""
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE]):
                return self.async_abort(reason="already_configured")
            return self.async_create_entry(
                title=self._title(user_input),
                data={
                    CONF_DEVICE: user_input[CONF_DEVICE],
                    CONF_KIND: user_input.get(CONF_KIND) or "auto",
                },
            )
        return self.async_show_form(
            step_id="user", data_schema=self._schema().extend(self._kind_field("auto"))
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change device, name, kind and the settings of the kind."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE], subentry.subentry_id):
                return self.async_abort(reason="already_configured")
            data = {k: v for k, v in user_input.items() if k != "name"}
            for key in ("reminder", "solar_min", "door_minutes"):
                if data.get(key) is not None:
                    data[key] = int(data[key])
            return self.async_update_and_abort(
                self._get_entry(), subentry, title=self._title(user_input), data=data
            )
        current = dict(subentry.data)
        schema = (
            self._schema(current.get(CONF_DEVICE), subentry.title)
            .extend(self._kind_field(current.get(CONF_KIND) or "auto"))
            .extend(self._options_schema(self._resolved_kind(current), current))
        )
        return self.async_show_form(step_id="reconfigure", data_schema=schema)


class PartSubentryFlow(ConfigSubentryFlow):
    """Add or change a maintenance part (carbon filter, backwash filter, smoke detector …)."""

    def __init__(self) -> None:
        """Initialize the flow."""
        self._data: dict[str, Any] = {}

    def _base_schema(self, cur: dict[str, Any], name: str | None = None) -> dict[Any, Any]:
        return {
            vol.Required(PART_KIND, default=cur.get(PART_KIND) or "carbon_filter"): SelectSelector(
                SelectSelectorConfig(
                    options=PART_KINDS,
                    translation_key="part_kind",
                    mode=SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_AREA, description={"suggested_value": cur.get(CONF_AREA)}): (
                AreaSelector()
            ),
            vol.Optional(CONF_DEVICE, description={"suggested_value": cur.get(CONF_DEVICE)}): (
                DeviceSelector()
            ),
            vol.Optional("name", description={"suggested_value": name}): TextSelector(),
        }

    def _interval_schema(self, kind: str, cur: dict[str, Any]) -> dict[Any, Any]:
        fields: dict[Any, Any] = {}
        for action in ("clean", "replace"):
            key = f"{action}_every"
            value = cur.get(key)
            fields[
                vol.Required(key, default=default_months(kind, action) if value is None else value)
            ] = NumberSelector(
                NumberSelectorConfig(
                    min=0,
                    max=240,
                    step=1,
                    unit_of_measurement="months",
                    mode=NumberSelectorMode.BOX,
                )
            )
        return fields

    def _title(self, data: dict[str, Any], name: str | None) -> str:
        if name := (name or "").strip():
            return name
        label = PART_SPECS.get(data.get(PART_KIND) or "other", PART_SPECS["other"])["label"]
        area = (
            ar.async_get(self.hass).async_get_area(data[CONF_AREA]) if data.get(CONF_AREA) else None
        )
        return f"{label} {area.name}" if area else label

    @staticmethod
    def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
        data = {k: v for k, v in user_input.items() if k != "name"}
        for key in ("clean_every", "replace_every"):
            if data.get(key) is not None:
                data[key] = int(data[key])
        for key in (CONF_AREA, CONF_DEVICE):
            if key in data:
                data[key] = data[key] or None
        return data

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick kind, area, device and name."""
        if user_input is not None:
            self._data = {**self._clean(user_input), "name": user_input.get("name")}
            return await self.async_step_intervals()
        return self.async_show_form(step_id="user", data_schema=vol.Schema(self._base_schema({})))

    async def async_step_intervals(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """How often to clean and to replace (suggested by the kind)."""
        if user_input is not None:
            name = self._data.pop("name", None)
            data = {**self._data, **self._clean(user_input), CONF_SINCE: now_ms()}
            return self.async_create_entry(title=self._title(data, name), data=data)
        return self.async_show_form(
            step_id="intervals",
            data_schema=vol.Schema(self._interval_schema(self._data[PART_KIND], {})),
            description_placeholders={
                "kind": PART_SPECS[self._data[PART_KIND]]["label"],
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change everything (when it was last done stays)."""
        subentry = self._get_reconfigure_subentry()
        cur = dict(subentry.data)
        if user_input is not None:
            data = {**cur, **self._clean(user_input)}
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=self._title(data, user_input.get("name")),
                data=data,
            )
        kind = cur.get(PART_KIND) or "other"
        schema = {**self._base_schema(cur, subentry.title), **self._interval_schema(kind, cur)}
        return self.async_show_form(step_id="reconfigure", data_schema=vol.Schema(schema))


class DoorbellSubentryFlow(MowerSubentryFlow):
    """Add or change a doorbell: device, optionally name, camera and lock."""

    _type = SUBENTRY_DOORBELL
    _default = "Klingel"

    def _schema(self, device: str | None = None, name: str | None = None) -> vol.Schema:
        cur = getattr(self, "_current", {})
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE, default=device or vol.UNDEFINED): DeviceSelector(
                    DeviceSelectorConfig(
                        entity=[
                            EntityFilterSelectorConfig(domain="event"),
                            EntityFilterSelectorConfig(domain="binary_sensor"),
                        ]
                    )
                ),
                vol.Optional("name", description={"suggested_value": name}): TextSelector(),
                vol.Optional("camera", description={"suggested_value": cur.get("camera")}): (
                    EntitySelector(EntitySelectorConfig(domain="camera"))
                ),
                vol.Optional("lock", description={"suggested_value": cur.get("lock")}): (
                    EntitySelector(EntitySelectorConfig(domain="lock"))
                ),
            }
        )

    @staticmethod
    def _data(user_input: dict[str, Any]) -> dict[str, Any]:
        return {
            CONF_DEVICE: user_input[CONF_DEVICE],
            "camera": user_input.get("camera") or None,
            "lock": user_input.get("lock") or None,
        }

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Add a doorbell."""
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE]):
                return self.async_abort(reason="already_configured")
            return self.async_create_entry(
                title=self._title(user_input), data=self._data(user_input)
            )
        return self.async_show_form(step_id="user", data_schema=self._schema())

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change device, name, camera or lock."""
        subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE], subentry.subentry_id):
                return self.async_abort(reason="already_configured")
            return self.async_update_and_abort(
                self._get_entry(),
                subentry,
                title=self._title(user_input),
                data=self._data(user_input),
            )
        self._current = dict(subentry.data)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(subentry.data.get(CONF_DEVICE), subentry.title),
        )


class DeviceSubentryFlow(ConfigSubentryFlow):
    """Give a device a role (inverter, battery, heat pump, gateway …) – then adjust its entities."""

    def _schema(self, cur: dict[str, Any] | None = None, name: str | None = None) -> vol.Schema:
        cur = cur or {}
        return vol.Schema(
            {
                vol.Required(
                    CONF_DEVICE, default=cur.get(CONF_DEVICE) or vol.UNDEFINED
                ): DeviceSelector(DeviceSelectorConfig()),
                vol.Required(
                    CONF_ROLE, default=cur.get(CONF_ROLE) or vol.UNDEFINED
                ): SelectSelector(
                    SelectSelectorConfig(
                        options=ROLES, translation_key=CONF_ROLE, mode=SelectSelectorMode.DROPDOWN
                    )
                ),
                vol.Optional("name", description={"suggested_value": name}): TextSelector(),
            }
        )

    def _catalog(self) -> Catalog:
        return Catalog(self.hass, self._get_entry(), lambda: False)

    def _entities_schema(self) -> vol.Schema:
        catalog = self._catalog()
        device_id, role = self._data[CONF_DEVICE], self._data[CONF_ROLE]
        detected = catalog.detect(role, device_id)
        own = self._data.get(CONF_ENTITIES) or {}
        include = [e.entity_id for e in catalog.device_entities(device_id)]
        fields: dict[Any, Any] = {}
        for key, spec in KEYS[role].items():
            value = own.get(key) or detected.get(key)
            fields[vol.Optional(key, description={"suggested_value": value})] = EntitySelector(
                EntitySelectorConfig(domain=list(spec.domains), include_entities=include)
            )
        if role == "battery":
            fields[
                vol.Optional(
                    CONF_CAPACITY, description={"suggested_value": self._data.get(CONF_CAPACITY)}
                )
            ] = NumberSelector(
                NumberSelectorConfig(
                    min=0.5,
                    max=200,
                    step=0.1,
                    unit_of_measurement="kWh",
                    mode=NumberSelectorMode.BOX,
                )
            )
        return vol.Schema(fields)

    def _title(self, name: str | None) -> str:
        if name := (name or "").strip():
            return name
        device = dr.async_get(self.hass).async_get(self._data[CONF_DEVICE])
        return (
            (device.name_by_user or device.name) if device else ROLE_LABELS[self._data[CONF_ROLE]]
        )

    def _finish_data(self, user_input: dict[str, Any]) -> dict[str, Any]:
        detected = self._catalog().detect(self._data[CONF_ROLE], self._data[CONF_DEVICE])
        own = {
            k: v
            for k, v in user_input.items()
            if k in KEYS[self._data[CONF_ROLE]] and v and detected.get(k) != v
        }
        data = {
            CONF_DEVICE: self._data[CONF_DEVICE],
            CONF_ROLE: self._data[CONF_ROLE],
            CONF_ENTITIES: own,
        }
        if user_input.get(CONF_CAPACITY):
            data[CONF_CAPACITY] = float(user_input[CONF_CAPACITY])
        return data

    def _taken(self, device_id: str, own: str | None = None) -> bool:
        return any(
            sub.subentry_type == SUBENTRY_DEVICE
            and sub.data.get(CONF_DEVICE) == device_id
            and sub.subentry_id != own
            for sub in self._get_entry().subentries.values()
        )

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> SubentryFlowResult:
        """Pick the device and its role."""
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE]):
                return self.async_abort(reason="already_configured")
            self._data = dict(user_input)
            self._name = user_input.get("name")
            return await self.async_step_entities()
        return self.async_show_form(step_id="user", data_schema=self._schema())

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Check (and change) which entity is what – found automatically."""
        if user_input is not None:
            data = self._finish_data(user_input)
            if getattr(self, "_subentry", None) is not None:
                return self.async_update_and_abort(
                    self._get_entry(), self._subentry, title=self._title(self._name), data=data
                )
            return self.async_create_entry(title=self._title(self._name), data=data)
        return self.async_show_form(
            step_id="entities",
            data_schema=self._entities_schema(),
            description_placeholders={"role": ROLE_LABELS[self._data[CONF_ROLE]]},
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> SubentryFlowResult:
        """Change device, role or name – then the entities."""
        self._subentry = self._get_reconfigure_subentry()
        if user_input is not None:
            if self._taken(user_input[CONF_DEVICE], self._subentry.subentry_id):
                return self.async_abort(reason="already_configured")
            self._data = {**self._subentry.data, **user_input}
            self._name = user_input.get("name")
            return await self.async_step_entities()
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self._schema(dict(self._subentry.data), self._subentry.title),
        )
