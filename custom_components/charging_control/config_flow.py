"""Config flow for Charging Control integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

_LOGGER = logging.getLogger(__name__)

DOMAIN = "charging_control"


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Charging Control."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            # Validate that at least the required entities are provided
            required_entities = [
                "max_import_power_entity",
                "avg_import_power_15min_entity",
                "current_l1_entity",
                "voltage_l1_entity",
            ]
            
            missing_entities = [
                entity for entity in required_entities
                if not user_input.get(entity)
            ]
            
            if missing_entities:
                errors["base"] = "missing_entities"
            else:
                # Create the config entry
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                
                return self.async_create_entry(
                    title="Charging Control",
                    data=user_input,
                )

        # Show the configuration form
        data_schema = vol.Schema(
            {
                vol.Required("max_import_power_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required("avg_import_power_15min_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required("current_l1_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("current_l2_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("current_l3_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required("voltage_l1_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("voltage_l2_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("voltage_l3_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("charger_current_l1_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("charger_current_l2_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("charger_current_l3_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional("update_interval", default=10): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=60, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional("charger_switch_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional("charger_current_select_entity"): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["input_select", "select"])
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> OptionsFlow:
        """Get the options flow for this handler."""
        return OptionsFlow(config_entry)


class OptionsFlow(config_entries.OptionsFlow):
    """Handle options for Charging Control."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Get current values
        current_data = self.config_entry.data

        data_schema = vol.Schema(
            {
                vol.Required(
                    "max_import_power_entity",
                    default=current_data.get("max_import_power_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    "avg_import_power_15min_entity",
                    default=current_data.get("avg_import_power_15min_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    "current_l1_entity",
                    default=current_data.get("current_l1_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "current_l2_entity",
                    default=current_data.get("current_l2_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "current_l3_entity",
                    default=current_data.get("current_l3_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Required(
                    "voltage_l1_entity",
                    default=current_data.get("voltage_l1_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "voltage_l2_entity",
                    default=current_data.get("voltage_l2_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "voltage_l3_entity",
                    default=current_data.get("voltage_l3_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "charger_current_l1_entity",
                    default=current_data.get("charger_current_l1_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "charger_current_l2_entity",
                    default=current_data.get("charger_current_l2_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "charger_current_l3_entity",
                    default=current_data.get("charger_current_l3_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="sensor")
                ),
                vol.Optional(
                    "update_interval",
                    default=current_data.get("update_interval", 10),
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=5, max=60, step=1, mode=selector.NumberSelectorMode.BOX
                    )
                ),
                vol.Optional(
                    "charger_switch_entity",
                    default=current_data.get("charger_switch_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain="switch")
                ),
                vol.Optional(
                    "charger_current_select_entity",
                    default=current_data.get("charger_current_select_entity"),
                ): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["input_select", "select"])
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=data_schema,
        )