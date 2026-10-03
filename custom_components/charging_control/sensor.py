"""Sensor platform for Charging Control."""

from __future__ import annotations

import logging
import math
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

DOMAIN = "charging_control"


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform."""
    # Get merged config (data + options) from hass.data
    config = hass.data[DOMAIN][config_entry.entry_id]

    sensors = [
        ChargingAllowedSensor(hass, config, config_entry.entry_id),
        MaxChargingCurrentSensor(hass, config, config_entry.entry_id),
    ]

    async_add_entities(sensors, True)


# Length of the short averaging window used to estimate the base load.
POWER_WINDOW_SECONDS = 30
# After changing the charger current, wait this long before changing it again so
# the averaging windows can settle on the new operating point, even when the
# charger current sensors lag the grid meter by a few update intervals.
SETTLE_SECONDS = 2 * POWER_WINDOW_SECONDS
# Only raise the charger current when the computed target exceeds the current
# setpoint by at least this many amps. Decreases are applied without a deadband.
INCREASE_DEADBAND_AMPS = 2
# Minimum charging current supported by most EV chargers (IEC 61851).
MIN_CHARGING_CURRENT = 6


class ControlState:
    """Runtime state shared by all sensors of one config entry.

    Both sensors run the control loop, so timers that gate charger actions must
    be shared or the second sensor would undo the first one's settle period.
    """

    def __init__(self) -> None:
        self.last_current_change: datetime | None = None
        self.over_limit_since: datetime | None = None


_CONTROL_STATE: dict[str, ControlState] = {}


def get_control_state(entry_id: str) -> ControlState:
    """Return the shared control state for a config entry."""
    return _CONTROL_STATE.setdefault(entry_id, ControlState())


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

    def __init__(
        self, hass: HomeAssistant, config: dict[str, Any], entry_id: str
    ) -> None:
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

        # Estimated power with charging entity (optional)
        # This entity provides an estimate of the 15-min average power if charging resumes at minimum speed
        self.estimated_power_with_charging_entity = config.get(
            "estimated_power_with_charging_entity"
        )

        # Power tracking. Total power and charger power are sampled at the same
        # instants and averaged over the same window so that subtracting one
        # from the other yields the base load without a lag-induced error.
        self.power_window_30s = PowerWindow(POWER_WINDOW_SECONDS)
        self.charger_power_window_30s = PowerWindow(POWER_WINDOW_SECONDS)
        self.power_window_15min = PowerWindow(15 * 60)

        # Timers shared between the sensors of this entry
        self._control_state = get_control_state(entry_id)
        # Grace period before a 15-min average above the limit stops charging.
        # Requires the condition to persist across at least two update ticks so a
        # single transient sample (e.g. at a quarter-hour reset) does not trip it.
        self.power_limit_grace_seconds = self.update_interval

        # State tracking for hysteresis
        self._charging_stopped_due_to_power_limit = False

        self._unsub_state_change = None
        self._unsub_interval = None
        self._last_update = None

    def _is_charging_enabled(self) -> bool:
        """Check if charging control is enabled via the switch."""
        # Try to find switch entity using entity registry
        entity_registry = er.async_get(self.hass)
        if entity_registry:
            expected_unique_id = f"{DOMAIN}_allow_charging_{self._entry_id}"
            for entity in entity_registry.entities.values():
                if entity.unique_id == expected_unique_id and entity.domain == "switch":
                    switch_state = self.hass.states.get(entity.entity_id)
                    if switch_state:
                        return switch_state.state == "on"

        # Switch not found, default to enabled
        _LOGGER.warning(f"Allow charging switch not found, defaulting to allow.")
        return True

    def _get_max_current_cap(self) -> int:
        """Get the user-selected maximum current cap."""
        # Try to find select entity using entity registry
        entity_registry = er.async_get(self.hass)
        if entity_registry:
            expected_unique_id = f"{DOMAIN}_max_charging_current_cap_{self._entry_id}"
            for entity in entity_registry.entities.values():
                if entity.unique_id == expected_unique_id and entity.domain in (
                    "select",
                    "input_select",
                ):
                    select_state = self.hass.states.get(entity.entity_id)
                    if select_state and select_state.state != "unavailable":
                        try:
                            return int(select_state.state)
                        except (ValueError, TypeError):
                            _LOGGER.warning(
                                f"Invalid max current cap value: {select_state.state}, using default 16A"
                            )
                            return 16

        # Select not found, default to 16A
        _LOGGER.warning(f"Charging current select not found, using default 16A")
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
                await self._control_charger_switch(
                    charging_enabled and charging_allowed
                )

            # Control charger current if configured
            if (
                self.charger_current_select_entity
                and charging_enabled
                and charging_allowed
            ):
                await self._control_charger_current(max_current)

        except Exception as e:
            _LOGGER.error(f"Error updating charger control: {e}")

    async def _control_charger_switch(self, should_charge: bool) -> None:
        """Control the charger switch entity."""
        try:
            current_state = self.hass.states.get(self.charger_switch_entity)
            if current_state is None:
                _LOGGER.warning(
                    f"Charger switch entity {self.charger_switch_entity} not found"
                )
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
                _LOGGER.debug(
                    f"Turned off charger switch: {self.charger_switch_entity}"
                )

        except Exception as e:
            _LOGGER.error(f"Error controlling charger switch: {e}")

    async def _control_charger_current(self, target_current: int) -> None:
        """Control the charger current select entity."""
        try:
            current_state = self.hass.states.get(self.charger_current_select_entity)
            if current_state is None:
                _LOGGER.warning(
                    f"Charger current select entity {self.charger_current_select_entity} not found"
                )
                return

            # Get available options
            options = current_state.attributes.get("options", [])
            if not options:
                _LOGGER.warning(
                    f"No options available for {self.charger_current_select_entity}"
                )
                return

            # Find the best matching option
            target_str = str(target_current)
            if target_str in options:
                selected_current, selected_option = target_current, target_str
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
            if current_state.state == selected_option:
                return

            if not self._should_change_current(current_state.state, selected_current):
                return

            domain = (
                "input_select"
                if self.charger_current_select_entity.startswith("input_select.")
                else "select"
            )
            await self.hass.services.async_call(
                domain,
                "select_option",
                {
                    "entity_id": self.charger_current_select_entity,
                    "option": selected_option,
                },
            )
            self._control_state.last_current_change = dt_util.now()
            _LOGGER.debug(
                f"Set charger current to {selected_option}A: {self.charger_current_select_entity}"
            )

        except Exception as e:
            _LOGGER.error(f"Error controlling charger current: {e}")

    def _should_change_current(self, current_option: str, target: int) -> bool:
        """Decide whether a computed target justifies changing the setpoint.

        Applies a settle time after every change and a deadband on increases so
        that the control loop does not chase its own measurement lag.
        """
        now = dt_util.now()
        last_change = self._control_state.last_current_change
        if last_change is not None:
            elapsed = (now - last_change).total_seconds()
            if elapsed < SETTLE_SECONDS:
                _LOGGER.debug(
                    f"Skipping charger current change to {target}A: "
                    f"settling for another {SETTLE_SECONDS - elapsed:.0f}s"
                )
                return False

        try:
            current = int(current_option)
        except (ValueError, TypeError):
            # Unknown current setpoint, apply the target unconditionally
            return True

        if target < current:
            return True
        # The deadband guards against chasing measurement lag near the available
        # power limit. A target pinned at the user cap is not driven by that
        # measurement, so the final step up to the cap is always allowed.
        if target >= self._get_max_current_cap():
            return True
        if target - current < INCREASE_DEADBAND_AMPS:
            _LOGGER.debug(
                f"Skipping charger current increase from {current}A to {target}A: "
                f"below {INCREASE_DEADBAND_AMPS}A deadband"
            )
            return False
        return True

    def _can_resume_charging(self, max_import: float) -> bool:
        """Check if charging can resume after being stopped due to power limit.

        If estimated_power_with_charging_entity is configured, resume when that
        estimated value drops below max_import.
        Otherwise, fall back to resuming when avg_import_15min < 90% of max_import.
        """
        if self.estimated_power_with_charging_entity:
            state = self.hass.states.get(self.estimated_power_with_charging_entity)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    estimated_power = float(state.state)
                    return estimated_power < max_import
                except (ValueError, TypeError):
                    _LOGGER.warning(
                        f"Could not convert estimated power state to float: {state.state}, "
                        "falling back to 90% threshold"
                    )
        # Fall back to 90% of max import threshold
        avg_import_15min = self._get_state_value(self.avg_import_entity)
        return avg_import_15min < max_import * 0.9

    def _calculate_charging_allowed(self) -> bool:
        """Calculate if charging should be allowed (without checking the switch)."""
        # Get the 15-minute average import power from entity
        avg_import_15min = self._get_state_value(self.avg_import_entity)

        # Get maximum allowed import power
        max_import = self._get_state_value(self.max_import_entity)

        if max_import <= 0:
            return False

        # Check if we should stop charging due to power limit
        # Only disable if charger is actually drawing power (not just connected and waiting)
        # and the limit has been exceeded for the whole grace period.
        if avg_import_15min >= max_import:
            if self._over_limit_for_grace_period():
                charger_power = self._calculate_charger_power()
                if charger_power > 0:
                    self._charging_stopped_due_to_power_limit = True
                    return False
        else:
            self._control_state.over_limit_since = None

        # If charging was previously stopped due to power limit,
        # check if we can resume
        if self._charging_stopped_due_to_power_limit:
            if self._can_resume_charging(max_import):
                # Clear the flag, we can resume charging
                self._charging_stopped_due_to_power_limit = False
            else:
                # Still can't resume, keep charging disabled
                return False

        # Charging is allowed if 15-min average is below the maximum
        return True

    def _over_limit_for_grace_period(self) -> bool:
        """Return True once the 15-min average has been over the limit long enough."""
        now = dt_util.now()
        state = self._control_state
        if state.over_limit_since is None:
            state.over_limit_since = now
            return False
        return (
            now - state.over_limit_since
        ).total_seconds() >= self.power_limit_grace_seconds

    def _calculate_base_power(self) -> float:
        """Estimate the household load excluding the charger.

        Both terms come from the same averaging window so a change in charger
        current does not skew the estimate while the window catches up.
        """
        now = dt_util.now()
        avg_power_30s = self.power_window_30s.get_average(now)
        avg_charger_power_30s = self.charger_power_window_30s.get_average(now)
        if avg_power_30s is None or avg_charger_power_30s is None:
            # No measurements yet, fall back to instantaneous values for both
            avg_power_30s = self._calculate_current_power() or 0
            avg_charger_power_30s = self._calculate_charger_power()
        return avg_power_30s - avg_charger_power_30s

    def _calculate_max_current(self) -> int:
        """Calculate maximum allowed charging current (without checking the switch)."""
        try:
            # Get maximum allowed import power
            max_import_power = self._get_state_value(self.max_import_entity)

            # Calculate power without current charging
            base_power = self._calculate_base_power()

            # Calculate available power for charging
            available_power = max_import_power - base_power

            if available_power <= 0:
                # Return minimum current instead of 0 (charging_allowed will handle stopping)
                return MIN_CHARGING_CURRENT

            # Get average voltage (use average of three phases)
            voltage_l1 = self._get_state_value(self.voltage_l1_entity, 230.0)
            voltage_l2 = self._get_state_value(self.voltage_l2_entity, 230.0)
            voltage_l3 = self._get_state_value(self.voltage_l3_entity, 230.0)
            avg_voltage = (voltage_l1 + voltage_l2 + voltage_l3) / 3

            # Calculate maximum current per phase (assuming balanced three-phase charging)
            # P = √3 * U * I for three-phase, so I = P / (√3 * U)
            # But since we want current per phase: I = P / (3 * U)
            max_current_per_phase = available_power / (3 * avg_voltage)

            # Convert to integer using floor and clamp between 6A and the cap
            max_current_int = math.floor(max_current_per_phase)

            # Get user-selected maximum current cap
            max_current_cap = self._get_max_current_cap()

            # Clamp to valid charging current range (6A to user-selected max)
            if max_current_int < MIN_CHARGING_CURRENT:
                # Always return at least 6A (charging_allowed will handle stopping)
                return MIN_CHARGING_CURRENT
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
            self.hass,
            self._update_power_measurements,
            timedelta(seconds=self.update_interval),
        )

        # Restore previous state
        if last_state := await self.async_get_last_state():
            if last_state.state not in ("unknown", "unavailable"):
                self._attr_native_value = last_state.state
            # Restore hysteresis state from attributes
            attrs = last_state.attributes or {}
            self._charging_stopped_due_to_power_limit = attrs.get(
                "charging_stopped_due_to_power_limit", False
            )

    async def async_will_remove_from_hass(self) -> None:
        """Handle entity being removed from hass."""
        if self._unsub_state_change:
            self._unsub_state_change()
        if self._unsub_interval:
            self._unsub_interval()
        _CONTROL_STATE.pop(self._entry_id, None)

    @callback
    def _handle_state_change(self, event) -> None:
        """Handle state changes of tracked entities."""
        # Only update if enough time has passed since last update
        now = dt_util.now()
        if (
            self._last_update is None
            or (now - self._last_update).total_seconds() >= self.update_interval
        ):
            self._last_update = now
            self.async_schedule_update_ha_state(True)

    @callback
    def _update_power_measurements(self, now) -> None:
        """Update power measurements periodically."""
        current_power = self._calculate_current_power()
        if current_power is not None:
            timestamp = dt_util.now()
            self.power_window_30s.add_measurement(current_power, timestamp)
            self.charger_power_window_30s.add_measurement(
                self._calculate_charger_power(), timestamp
            )
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
                _LOGGER.warning(
                    f"Could not convert state of {entity_id} to float: {state.state}"
                )
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

        # Use the base class method which includes hysteresis logic
        return self._calculate_charging_allowed()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        max_import = self._get_state_value(self.max_import_entity)
        attrs = {
            "charging_control_enabled": self._is_charging_enabled(),
            "avg_import_power_15min": self._get_state_value(self.avg_import_entity),
            "max_import_power": max_import,
            "current_power": self._calculate_current_power(),
            "charging_stopped_due_to_power_limit": self._charging_stopped_due_to_power_limit,
            "estimated_power_with_charging_configured": bool(
                self.estimated_power_with_charging_entity
            ),
        }
        # Show the resume threshold based on configuration
        if self.estimated_power_with_charging_entity:
            attrs["estimated_power_with_charging"] = self._get_state_value(
                self.estimated_power_with_charging_entity
            )
        else:
            attrs["restart_threshold"] = max_import * 0.9 if max_import > 0 else 0
        return attrs


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
        # First check if charging control is enabled
        if not self._is_charging_enabled():
            return 0

        return self._calculate_max_current()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        now = dt_util.now()
        avg_power_30s = self.power_window_30s.get_average(now)
        avg_charger_power_30s = self.charger_power_window_30s.get_average(now)
        charger_power = self._calculate_charger_power()
        max_import = self._get_state_value(self.max_import_entity)
        last_change = self._control_state.last_current_change

        attrs = {
            "charging_control_enabled": self._is_charging_enabled(),
            "max_current_cap": self._get_max_current_cap(),
            "avg_power_30s": avg_power_30s,
            "avg_charger_power_30s": avg_charger_power_30s,
            "current_charger_power": charger_power,
            "max_import_power": max_import,
            "base_power_without_charging": (
                (avg_power_30s - avg_charger_power_30s)
                if avg_power_30s is not None and avg_charger_power_30s is not None
                else None
            ),
            "last_current_change": last_change.isoformat() if last_change else None,
            "charger_switch_configured": bool(self.charger_switch_entity),
            "charger_current_select_configured": bool(
                self.charger_current_select_entity
            ),
            "charging_stopped_due_to_power_limit": self._charging_stopped_due_to_power_limit,
            "estimated_power_with_charging_configured": bool(
                self.estimated_power_with_charging_entity
            ),
        }
        # Show the resume threshold based on configuration
        if self.estimated_power_with_charging_entity:
            attrs["estimated_power_with_charging"] = self._get_state_value(
                self.estimated_power_with_charging_entity
            )
        else:
            attrs["restart_threshold"] = max_import * 0.9 if max_import > 0 else 0
        return attrs


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
