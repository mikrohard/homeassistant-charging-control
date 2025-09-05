"""Charging Control integration for Home Assistant."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

_LOGGER = logging.getLogger(__name__)

DOMAIN = "charging_control"
PLATFORMS = [Platform.SENSOR, Platform.SWITCH, Platform.SELECT]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Charging Control from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = entry.data

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register services
    async def handle_update_charger(call):
        """Handle the update_charger service call."""
        from .sensor import update_charger_from_calculations
        
        entry_id = call.data.get("entry_id")
        if not entry_id:
            # Find the first entry if no specific entry_id provided
            entries = hass.config_entries.async_entries(DOMAIN)
            if entries:
                entry_id = entries[0].entry_id
            else:
                _LOGGER.error("No charging control entries found")
                return
        
        if entry_id not in hass.data[DOMAIN]:
            _LOGGER.error(f"Entry {entry_id} not found")
            return
            
        await update_charger_from_calculations(hass, entry_id)

    hass.services.async_register(
        DOMAIN, "update_charger", handle_update_charger
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        
        # Remove services if this was the last entry
        if not hass.data[DOMAIN]:
            hass.services.async_remove(DOMAIN, "update_charger")

    return unload_ok


async def async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload config entry."""
    await async_unload_entry(hass, entry)
    await async_setup_entry(hass, entry)