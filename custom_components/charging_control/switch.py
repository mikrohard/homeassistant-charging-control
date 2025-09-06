"""Switch platform for Charging Control."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
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
    """Set up the switch platform."""
    config = config_entry.data
    
    switches = [
        ChargingEnabledSwitch(hass, config, config_entry.entry_id),
    ]
    
    async_add_entities(switches, True)


class ChargingEnabledSwitch(SwitchEntity, RestoreEntity):
    """Switch to manually enable/disable charging control."""
    
    _attr_icon = "mdi:power"
    _attr_has_entity_name = True
    
    def __init__(self, hass: HomeAssistant, config: dict[str, Any], entry_id: str) -> None:
        """Initialize the switch."""
        self.hass = hass
        self.config = config
        self._entry_id = entry_id
        self._attr_is_on = True  # Default to enabled
        self._attr_unique_id = f"{DOMAIN}_allow_charging_{self._entry_id}"
    
    @property
    def name(self) -> str:
        """Return the name of the switch."""
        return "Allow charging"
    
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
            if last_state.state in ("on", "off"):
                self._attr_is_on = last_state.state == "on"
    
    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        self._attr_is_on = True
        self.async_write_ha_state()
        _LOGGER.debug("Charging control enabled")
    
    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        self._attr_is_on = False
        self.async_write_ha_state()
        _LOGGER.debug("Charging control disabled")
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "description": "When disabled, charging is not allowed regardless of power calculations",
        }