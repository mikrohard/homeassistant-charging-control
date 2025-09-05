"""Select platform for Charging Control."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

_LOGGER = logging.getLogger(__name__)

DOMAIN = "charging_control"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the select platform."""
    config = config_entry.data
    
    selects = [
        MaxChargingCurrentSelect(hass, config, config_entry.entry_id),
    ]
    
    async_add_entities(selects, True)


class MaxChargingCurrentSelect(SelectEntity, RestoreEntity):
    """Select entity for maximum charging current cap."""
    
    _attr_icon = "mdi:speedometer"
    _attr_has_entity_name = True
    
    def __init__(self, hass: HomeAssistant, config: dict[str, Any], entry_id: str) -> None:
        """Initialize the select entity."""
        self.hass = hass
        self.config = config
        self._entry_id = entry_id
        
        # Generate options from 6A to 32A
        self._attr_options = [str(i) for i in range(6, 33)]
        self._attr_current_option = "16"  # Default to 16A
    
    @property
    def name(self) -> str:
        """Return the name of the select entity."""
        return "Max Charging Current"
    
    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{DOMAIN}_max_charging_current_cap_{self._entry_id}"
    
    @property
    def device_info(self) -> dict[str, Any]:
        """Return device information."""
        return {
            "identifiers": {(DOMAIN, self._entry_id)},
            "name": "Charging Control",
            "manufacturer": "Custom",
            "model": "Charging Control Integration",
        }
    
    async def async_added_to_hass(self) -> None:
        """Handle entity being added to hass."""
        await super().async_added_to_hass()
        
        # Restore previous state
        if last_state := await self.async_get_last_state():
            if last_state.state in self._attr_options:
                self._attr_current_option = last_state.state
            else:
                # If restored state is invalid, use default
                self._attr_current_option = "16"
    
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        if option in self._attr_options:
            self._attr_current_option = option
            self.async_write_ha_state()
            _LOGGER.debug(f"Max charging current cap set to {option}A")
        else:
            _LOGGER.warning(f"Invalid charging current option: {option}")
    
    @property
    def current_option(self) -> str:
        """Return the current selected option."""
        return self._attr_current_option
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "unit_of_measurement": "A",
            "description": f"Maximum allowed charging current cap. Current setting: {self._attr_current_option}A",
            "range": "6A - 32A",
        }