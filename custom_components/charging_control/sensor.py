"""Sensor platform for Charging Control."""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

DOMAIN = "charging_control"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    config = config_entry.data
    
    sensors = [
        ChargingAllowedSensor(hass, config, config_entry.entry_id),
        MaxChargingCurrentSensor(hass, config, config_entry.entry_id),
    ]
    
    async_add_entities(sensors, True)


class PowerWindow:
    """Track power measurements over a time window."""
    
    def __init__(self, window_seconds: int):
        """Initialize the power window."""
        self.window_seconds = window_seconds
        self.measurements = deque()
    
    def add_measurement(self, power: float, timestamp: datetime) -> None:
        """Add a power measurement."""
        self.measurements.append((power, timestamp))
        self._cleanup(timestamp)
    
    def _cleanup(self, current_time: datetime) -> None:
        """Remove old measurements outside the window."""
        cutoff = current_time - timedelta(seconds=self.window_seconds)
        while self.measurements and self.measurements[0][1] < cutoff:
            self.measurements.popleft()
    
    def get_average(self, current_time: datetime) -> float | None:
        """Get the average power over the window."""
        self._cleanup(current_time)
        if not self.measurements:
            return None
        return sum(p for p, _ in self.measurements) / len(self.measurements)
    
    def clear(self) -> None:
        """Clear all measurements."""
        self.measurements.clear()


class ChargingControlSensorBase(SensorEntity, RestoreEntity):
    """Base class for charging control sensors."""
    
    def __init__(self, hass: HomeAssistant, config: dict[str, Any], entry_id: str) -> None:
        """Initialize the sensor."""
        self.hass = hass
        self.config = config
        self._entry_id = entry_id
        self._attr_has_entity_name = True
        
        # Update interval from config (default 10 seconds)
        self.update_interval = config.get("update_interval", 10)
        
        # Entity IDs from config
        self.max_import_entity = config.get("max_import_power_entity")
        self.avg_import_entity = config.get("avg_import_power_15min_entity")
        self.current_l1_entity = config.get("current_l1_entity")
        self.current_l2_entity = config.get("current_l2_entity")
        self.current_l3_entity = config.get("current_l3_entity")
        self.voltage_l1_entity = config.get("voltage_l1_entity")
        self.voltage_l2_entity = config.get("voltage_l2_entity")
        self.voltage_l3_entity = config.get("voltage_l3_entity")
        self.charger_current_l1_entity = config.get("charger_current_l1_entity")
        self.charger_current_l2_entity = config.get("charger_current_l2_entity")
        self.charger_current_l3_entity = config.get("charger_current_l3_entity")
        
        # Charger control entities (optional)
        self.charger_switch_entity = config.get("charger_switch_entity")
        self.charger_current_select_entity = config.get("charger_current_select_entity")
        
        # Power tracking
        self.power_window_30s = PowerWindow(30)
        self.power_window_15min = PowerWindow(15 * 60)
        
        self._unsub_state_change = None
        self._unsub_interval = None
        self._last_update = None
    
    def _is_charging_enabled(self) -> bool:
        """Check if charging control is enabled via the switch."""
        switch_entity_id = f"switch.{DOMAIN}_charging_enabled_{self._entry_id}"
        switch_state = self.hass.states.get(switch_entity_id)
        
        if switch_state is None:
            # Switch not found, default to enabled
            return True
        
        return switch_state.state == "on"
    
    def _get_max_current_cap(self) -> int:
        """Get the user-selected maximum current cap."""
        select_entity_id = f"select.{DOMAIN}_max_charging_current_cap_{self._entry_id}"
        select_state = self.hass.states.get(select_entity_id)
        
        if select_state is None or select_state.state == "unavailable":
            # Select not found, default to 16A
            return 16
        
        try:
            return int(select_state.state)
        except (ValueError, TypeError):
            _LOGGER.warning(f"Invalid max current cap value: {select_state.state}, using default 16A")
            return 16
    
    async def _update_charger_control(self) -> None:
        """Update charger control entities based on calculations."""
        try:
            # Check if charging control is enabled
            charging_enabled = self._is_charging_enabled()
            
            # Get current calculations
            charging_allowed = self._calculate_charging_allowed()
            max_current = self._calculate_max_current()
            
            # Control charger switch if configured
            if self.charger_switch_entity:
                await self._control_charger_switch(charging_enabled and charging_allowed)
            
            # Control charger current if configured
            if self.charger_current_select_entity and charging_enabled and charging_allowed:
                await self._control_charger_current(max_current)
                
        except Exception as e:
            _LOGGER.error(f"Error updating charger control: {e}")
    
    async def _control_charger_switch(self, should_charge: bool) -> None:
        """Control the charger switch entity."""
        try:
            current_state = self.hass.states.get(self.charger_switch_entity)
            if current_state is None:
                _LOGGER.warning(f"Charger switch entity {self.charger_switch_entity} not found")
                return
            
            current_is_on = current_state.state == "on"
            
            if should_charge and not current_is_on:
                await self.hass.services.async_call(
                    "switch", "turn_on", {"entity_id": self.charger_switch_entity}
                )
                _LOGGER.debug(f"Turned on charger switch: {self.charger_switch_entity}")
            elif not should_charge and current_is_on:
                await self.hass.services.async_call(
                    "switch", "turn_off", {"entity_id": self.charger_switch_entity}
                )
                _LOGGER.debug(f"Turned off charger switch: {self.charger_switch_entity}")
                
        except Exception as e:
            _LOGGER.error(f"Error controlling charger switch: {e}")
    
    async def _control_charger_current(self, target_current: int) -> None:
        """Control the charger current select entity."""
        try:
            current_state = self.hass.states.get(self.charger_current_select_entity)
            if current_state is None:
                _LOGGER.warning(f"Charger current select entity {self.charger_current_select_entity} not found")
                return
            
            # Get available options
            options = current_state.attributes.get("options", [])
            if not options:
                _LOGGER.warning(f"No options available for {self.charger_current_select_entity}")
                return
            
            # Find the best matching option
            target_str = str(target_current)
            if target_str in options:
                selected_option = target_str
            else:
                # Find closest available option that's <= target_current
                available_currents = []
                for option in options:
                    try:
                        current_val = int(option)
                        if current_val <= target_current:
                            available_currents.append((current_val, option))
                    except ValueError:
                        continue
                
                if available_currents:
                    # Select the highest available current that's <= target
                    selected_current, selected_option = max(available_currents)
                else:
                    # If no suitable option found, don't change anything
                    return
            
            # Only update if different from current selection
            if current_state.state != selected_option:
                domain = "input_select" if self.charger_current_select_entity.startswith("input_select.") else "select"
                await self.hass.services.async_call(
                    domain, "select_option", 
                    {"entity_id": self.charger_current_select_entity, "option": selected_option}
                )
                _LOGGER.debug(f"Set charger current to {selected_option}A: {self.charger_current_select_entity}")
                
        except Exception as e:
            _LOGGER.error(f"Error controlling charger current: {e}")
    
    def _calculate_charging_allowed(self) -> bool:
        """Calculate if charging should be allowed (without checking the switch)."""
        # Get the 15-minute average import power from entity
        avg_import_15min = self._get_state_value(self.avg_import_entity)
        
        # Get maximum allowed import power
        max_import = self._get_state_value(self.max_import_entity)
        
        if max_import <= 0:
            return False
        
        # Charging is allowed if 15-min average is below the maximum
        return avg_import_15min < max_import
    
    def _calculate_max_current(self) -> int:
        """Calculate maximum allowed charging current (without checking the switch)."""
        try:
            # Get maximum allowed import power
            max_import_power = self._get_state_value(self.max_import_entity)
            
            # Get 30-second average power
            avg_power_30s = self.power_window_30s.get_average(dt_util.now())
            if avg_power_30s is None:
                # If no measurements yet, use current power
                avg_power_30s = self._calculate_current_power() or 0
            
            # Get current charger power
            charger_power = self._calculate_charger_power()
            
            # Calculate power without current charging
            base_power = avg_power_30s - charger_power
            
            # Calculate available power for charging
            available_power = max_import_power - base_power
            
            if available_power <= 0:
                return 0
            
            # Get average voltage (use average of three phases)
            voltage_l1 = self._get_state_value(self.voltage_l1_entity, 230.0)
            voltage_l2 = self._get_state_value(self.voltage_l2_entity, 230.0)
            voltage_l3 = self._get_state_value(self.voltage_l3_entity, 230.0)
            avg_voltage = (voltage_l1 + voltage_l2 + voltage_l3) / 3
            
            # Calculate maximum current per phase (assuming balanced three-phase charging)
            # P = √3 * U * I for three-phase, so I = P / (√3 * U)
            # But since we want current per phase: I = P / (3 * U)
            max_current_per_phase = available_power / (3 * avg_voltage)
            
            # Convert to integer using floor and clamp between 6-32 amps
            import math
            max_current_int = int(math.floor(max_current_per_phase))
            
            # Get user-selected maximum current cap
            max_current_cap = self._get_max_current_cap()
            
            # Clamp to valid charging current range (6A to user-selected max)
            if max_current_int < 6:
                return 6 if available_power > 0 else 0
            elif max_current_int > max_current_cap:
                return max_current_cap
            else:
                return max_current_int
                
        except Exception as e:
            _LOGGER.error(f"Error calculating max charging current: {e}")
            return 0
    
    async def async_added_to_hass(self) -> None:
        """Handle entity being added to hass."""
        await super().async_added_to_hass()
        
        # Track all entity state changes
        entities_to_track = [
            self.max_import_entity,
            self.avg_import_entity,
            self.current_l1_entity,
            self.current_l2_entity,
            self.current_l3_entity,
            self.voltage_l1_entity,
            self.voltage_l2_entity,
            self.voltage_l3_entity,
            self.charger_current_l1_entity,
            self.charger_current_l2_entity,
            self.charger_current_l3_entity,
        ]
        
        entities_to_track = [e for e in entities_to_track if e]
        
        if entities_to_track:
            self._unsub_state_change = async_track_state_change_event(
                self.hass, entities_to_track, self._handle_state_change
            )
        
        # Update power measurements at configured interval
        self._unsub_interval = async_track_time_interval(
            self.hass, self._update_power_measurements, timedelta(seconds=self.update_interval)
        )
        
        # Restore previous state
        if last_state := await self.async_get_last_state():
            if last_state.state not in ("unknown", "unavailable"):
                self._attr_native_value = last_state.state
    
    async def async_will_remove_from_hass(self) -> None:
        """Handle entity being removed from hass."""
        if self._unsub_state_change:
            self._unsub_state_change()
        if self._unsub_interval:
            self._unsub_interval()
    
    @callback
    def _handle_state_change(self, event) -> None:
        """Handle state changes of tracked entities."""
        # Only update if enough time has passed since last update
        now = dt_util.now()
        if (self._last_update is None or 
            (now - self._last_update).total_seconds() >= self.update_interval):
            self._last_update = now
            self.async_schedule_update_ha_state(True)
    
    @callback
    def _update_power_measurements(self, now) -> None:
        """Update power measurements periodically."""
        current_power = self._calculate_current_power()
        if current_power is not None:
            timestamp = dt_util.now()
            self.power_window_30s.add_measurement(current_power, timestamp)
            self.power_window_15min.add_measurement(current_power, timestamp)
        
        self._last_update = dt_util.now()
        
        # Update charger if control entities are configured
        if self.charger_switch_entity or self.charger_current_select_entity:
            self.hass.async_create_task(self._update_charger_control())
        
        self.async_schedule_update_ha_state(True)
    
    def _get_state_value(self, entity_id: str, default: float = 0.0) -> float:
        """Get numeric value from entity state."""
        if not entity_id:
            return default
        
        state = self.hass.states.get(entity_id)
        if state and state.state not in ("unknown", "unavailable"):
            try:
                return float(state.state)
            except (ValueError, TypeError):
                _LOGGER.warning(f"Could not convert state of {entity_id} to float: {state.state}")
        return default
    
    def _calculate_current_power(self) -> float | None:
        """Calculate current total power consumption."""
        try:
            # Get current and voltage for each phase
            current_l1 = self._get_state_value(self.current_l1_entity)
            current_l2 = self._get_state_value(self.current_l2_entity)
            current_l3 = self._get_state_value(self.current_l3_entity)
            
            voltage_l1 = self._get_state_value(self.voltage_l1_entity, 230.0)
            voltage_l2 = self._get_state_value(self.voltage_l2_entity, 230.0)
            voltage_l3 = self._get_state_value(self.voltage_l3_entity, 230.0)
            
            # Calculate power for each phase (P = U * I)
            power_l1 = voltage_l1 * current_l1
            power_l2 = voltage_l2 * current_l2
            power_l3 = voltage_l3 * current_l3
            
            # Total power (positive = import, negative = export)
            total_power = power_l1 + power_l2 + power_l3
            
            return total_power
        except Exception as e:
            _LOGGER.error(f"Error calculating current power: {e}")
            return None
    
    def _calculate_charger_power(self) -> float:
        """Calculate current charger power consumption."""
        try:
            # Get charger current for each phase
            charger_l1 = self._get_state_value(self.charger_current_l1_entity)
            charger_l2 = self._get_state_value(self.charger_current_l2_entity)
            charger_l3 = self._get_state_value(self.charger_current_l3_entity)
            
            # Get voltage for each phase
            voltage_l1 = self._get_state_value(self.voltage_l1_entity, 230.0)
            voltage_l2 = self._get_state_value(self.voltage_l2_entity, 230.0)
            voltage_l3 = self._get_state_value(self.voltage_l3_entity, 230.0)
            
            # Calculate charger power for each phase
            charger_power_l1 = voltage_l1 * charger_l1
            charger_power_l2 = voltage_l2 * charger_l2
            charger_power_l3 = voltage_l3 * charger_l3
            
            # Total charger power
            total_charger_power = charger_power_l1 + charger_power_l2 + charger_power_l3
            
            return total_charger_power
        except Exception as e:
            _LOGGER.error(f"Error calculating charger power: {e}")
            return 0.0


class ChargingAllowedSensor(ChargingControlSensorBase):
    """Sensor that indicates if charging is allowed."""
    
    _attr_icon = "mdi:ev-station"
    
    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Charging Allowed"
    
    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{DOMAIN}_charging_allowed"
    
    @property
    def native_value(self) -> bool:
        """Return true if charging is allowed."""
        # First check if charging control is enabled
        if not self._is_charging_enabled():
            return False
        
        # Get the 15-minute average import power from entity
        avg_import_15min = self._get_state_value(self.avg_import_entity)
        
        # Get maximum allowed import power
        max_import = self._get_state_value(self.max_import_entity)
        
        if max_import <= 0:
            return False
        
        # Charging is allowed if 15-min average is below the maximum
        return avg_import_15min < max_import
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        return {
            "charging_control_enabled": self._is_charging_enabled(),
            "avg_import_power_15min": self._get_state_value(self.avg_import_entity),
            "max_import_power": self._get_state_value(self.max_import_entity),
            "current_power": self._calculate_current_power(),
        }


class MaxChargingCurrentSensor(ChargingControlSensorBase):
    """Sensor that calculates maximum allowed charging current."""
    
    _attr_icon = "mdi:current-ac"
    _attr_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_state_class = SensorStateClass.MEASUREMENT
    
    @property
    def name(self) -> str:
        """Return the name of the sensor."""
        return "Max Charging Current"
    
    @property
    def unique_id(self) -> str:
        """Return unique ID."""
        return f"{DOMAIN}_max_charging_current"
    
    @property
    def native_value(self) -> float:
        """Calculate maximum allowed charging current."""
        try:
            # First check if charging control is enabled
            if not self._is_charging_enabled():
                return 0
            
            # Get maximum allowed import power
            max_import_power = self._get_state_value(self.max_import_entity)
            
            # Get 30-second average power
            avg_power_30s = self.power_window_30s.get_average(dt_util.now())
            if avg_power_30s is None:
                # If no measurements yet, use current power
                avg_power_30s = self._calculate_current_power() or 0
            
            # Get current charger power
            charger_power = self._calculate_charger_power()
            
            # Calculate power without current charging
            base_power = avg_power_30s - charger_power
            
            # Calculate available power for charging
            available_power = max_import_power - base_power
            
            if available_power <= 0:
                return 0.0
            
            # Get average voltage (use average of three phases)
            voltage_l1 = self._get_state_value(self.voltage_l1_entity, 230.0)
            voltage_l2 = self._get_state_value(self.voltage_l2_entity, 230.0)
            voltage_l3 = self._get_state_value(self.voltage_l3_entity, 230.0)
            avg_voltage = (voltage_l1 + voltage_l2 + voltage_l3) / 3
            
            # Calculate maximum current per phase (assuming balanced three-phase charging)
            # P = √3 * U * I for three-phase, so I = P / (√3 * U)
            # But since we want current per phase: I = P / (3 * U)
            max_current_per_phase = available_power / (3 * avg_voltage)
            
            # Convert to integer using floor and clamp between 6-32 amps
            import math
            max_current_int = int(math.floor(max_current_per_phase))
            
            # Get user-selected maximum current cap
            max_current_cap = self._get_max_current_cap()
            
            # Clamp to valid charging current range (6A to user-selected max)
            if max_current_int < 6:
                return 6 if available_power > 0 else 0
            elif max_current_int > max_current_cap:
                return max_current_cap
            else:
                return max_current_int
            
        except Exception as e:
            _LOGGER.error(f"Error calculating max charging current: {e}")
            return 0.0
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        avg_power_30s = self.power_window_30s.get_average(dt_util.now())
        charger_power = self._calculate_charger_power()
        
        return {
            "charging_control_enabled": self._is_charging_enabled(),
            "max_current_cap": self._get_max_current_cap(),
            "avg_power_30s": avg_power_30s,
            "current_charger_power": charger_power,
            "max_import_power": self._get_state_value(self.max_import_entity),
            "base_power_without_charging": (avg_power_30s - charger_power) if avg_power_30s else None,
            "charger_switch_configured": bool(self.charger_switch_entity),
            "charger_current_select_configured": bool(self.charger_current_select_entity),
        }


async def update_charger_from_calculations(hass: HomeAssistant, entry_id: str) -> None:
    """Service function to manually update charger based on current calculations."""
    # Get the config data
    if entry_id not in hass.data.get(DOMAIN, {}):
        _LOGGER.error(f"Entry {entry_id} not found in domain data")
        return
    
    config = hass.data[DOMAIN][entry_id]
    
    # Create a temporary sensor instance to access the control methods
    temp_sensor = MaxChargingCurrentSensor(hass, config, entry_id)
    
    # Update charger control
    await temp_sensor._update_charger_control()