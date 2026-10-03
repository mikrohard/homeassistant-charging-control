"""Tests for the charger control loop in the sensor platform."""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
    async_mock_service,
)

from custom_components.charging_control.sensor import DOMAIN

UPDATE_INTERVAL = 10
VOLTAGE = 235.0
MAX_IMPORT = 11100
CHARGER_SELECT = "input_select.charger_current"
CHARGER_SWITCH = "switch.charger"

CONFIG = {
    "max_import_power_entity": "sensor.max_import",
    "avg_import_power_15min_entity": "sensor.avg_import_15min",
    "current_l1_entity": "sensor.current_l1",
    "current_l2_entity": "sensor.current_l2",
    "current_l3_entity": "sensor.current_l3",
    "voltage_l1_entity": "sensor.voltage_l1",
    "voltage_l2_entity": "sensor.voltage_l2",
    "voltage_l3_entity": "sensor.voltage_l3",
    "charger_current_l1_entity": "sensor.charger_l1",
    "charger_current_l2_entity": "sensor.charger_l2",
    "charger_current_l3_entity": "sensor.charger_l3",
    "charger_switch_entity": CHARGER_SWITCH,
    "charger_current_select_entity": CHARGER_SELECT,
    "update_interval": UPDATE_INTERVAL,
}


class Plant:
    """Simulated grid meter and charger.

    The charger draws exactly the selected current on all three phases. The grid
    meter sees the base load plus the charger. The charger's own current sensors
    may lag the meter by a number of update ticks, as real chargers polled over
    a slow link do.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        base_w: float,
        setpoint: int,
        charger_lag_ticks: int = 0,
    ):
        self.hass = hass
        self.base_w = base_w
        self.setpoint = setpoint
        self.avg_import_15min = 8000.0
        self.lag = charger_lag_ticks
        self.history = [setpoint] * (charger_lag_ticks + 1)
        self.setpoints: list[int] = []
        self.publish()

    def publish(self) -> None:
        """Write the current plant state to Home Assistant."""
        per_phase = self.base_w / (3 * VOLTAGE) + self.setpoint
        reported = self.history[0]
        states = {
            "sensor.max_import": MAX_IMPORT,
            "sensor.avg_import_15min": self.avg_import_15min,
            "sensor.current_l1": per_phase,
            "sensor.current_l2": per_phase,
            "sensor.current_l3": per_phase,
            "sensor.voltage_l1": VOLTAGE,
            "sensor.voltage_l2": VOLTAGE,
            "sensor.voltage_l3": VOLTAGE,
            "sensor.charger_l1": reported,
            "sensor.charger_l2": reported,
            "sensor.charger_l3": reported,
        }
        for entity_id, value in states.items():
            self.hass.states.async_set(entity_id, str(value))
        self.hass.states.async_set(
            CHARGER_SELECT,
            str(self.setpoint),
            {"options": [str(a) for a in range(6, 33)]},
        )
        if self.hass.states.get(CHARGER_SWITCH) is None:
            self.hass.states.async_set(CHARGER_SWITCH, "on")

    async def tick(self, freezer, count: int = 1) -> None:
        """Advance the simulation by ``count`` update intervals."""
        for _ in range(count):
            self.history.append(self.setpoint)
            self.history = self.history[-(self.lag + 1) :]
            self.publish()
            freezer.tick(timedelta(seconds=UPDATE_INTERVAL))
            async_fire_time_changed(self.hass, dt_util.utcnow())
            await self.hass.async_block_till_done()
            self.setpoints.append(self.setpoint)


@pytest.fixture
async def plant_factory(hass: HomeAssistant):
    """Return a factory that sets up the integration around a simulated plant."""

    async def _factory(
        base_w: float = 1000.0, setpoint: int = 9, charger_lag_ticks: int = 0
    ) -> Plant:
        plant = Plant(hass, base_w, setpoint, charger_lag_ticks)

        async def handle_select_option(call: ServiceCall) -> None:
            plant.setpoint = int(call.data["option"])
            plant.publish()

        hass.services.async_register(
            "input_select", "select_option", handle_select_option
        )

        entry = MockConfigEntry(domain=DOMAIN, data=CONFIG, entry_id="test_entry")
        entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        return plant

    return _factory


async def set_current_cap(hass: HomeAssistant, cap: int) -> None:
    """Set the integration's own max current cap select."""
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id(
        "select", DOMAIN, f"{DOMAIN}_max_charging_current_cap_test_entry"
    )
    assert entity_id is not None
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": entity_id, "option": str(cap)},
        blocking=True,
    )


@pytest.mark.parametrize("charger_lag_ticks", [0, 1, 2])
async def test_setpoint_settles_after_cap_is_raised(
    hass: HomeAssistant, freezer, plant_factory, charger_lag_ticks
):
    """Raising the cap must produce one step to the new target, not an oscillation.

    This reproduces the field failure where the current bounced between two
    values every few ticks once it was no longer clamped by the cap.
    """
    plant = await plant_factory(
        base_w=1000.0, setpoint=9, charger_lag_ticks=charger_lag_ticks
    )
    await set_current_cap(hass, 9)
    await plant.tick(freezer, 8)
    assert set(plant.setpoints) == {9}

    await set_current_cap(hass, 16)
    plant.setpoints.clear()
    await plant.tick(freezer, 40)

    # Available power is 11100 - 1000 = 10100 W -> floor(10100 / (3 * 235)) = 14 A
    assert plant.setpoints[0] == 14
    assert set(plant.setpoints[-30:]) == {14}, plant.setpoints


async def test_setpoint_follows_base_load_increase(
    hass: HomeAssistant, freezer, plant_factory
):
    """A step in household load lowers the setpoint once and holds it there."""
    plant = await plant_factory(base_w=1000.0, setpoint=14)
    await plant.tick(freezer, 8)

    plant.base_w += 3000.0
    plant.setpoints.clear()
    await plant.tick(freezer, 40)

    # Available power is 11100 - 4000 = 7100 W -> floor(7100 / 705) = 10 A
    assert plant.setpoints[-1] == 10
    changes = [b for a, b in zip(plant.setpoints, plant.setpoints[1:]) if a != b]
    assert len(changes) <= 2, plant.setpoints
    assert all(
        a >= b for a, b in zip(plant.setpoints, plant.setpoints[1:])
    ), plant.setpoints


async def test_increase_within_deadband_is_ignored(
    hass: HomeAssistant, freezer, plant_factory
):
    """A computed target only 1 A above the setpoint does not trigger a change."""
    # Available 10100 W -> 14 A target, so a 13 A setpoint sits inside the deadband
    plant = await plant_factory(base_w=1000.0, setpoint=13)
    await plant.tick(freezer, 20)
    assert set(plant.setpoints) == {13}, plant.setpoints


async def test_final_step_to_cap_ignores_deadband(
    hass: HomeAssistant, freezer, plant_factory
):
    """A setpoint 1 A below the cap still steps up to the cap.

    Reproduces the field failure where the charger sat at 15 A under a 16 A cap
    with ample headroom, because the 1 A step was swallowed by the deadband.
    """
    # Available 11100 - 100 = 11000 W -> 15 A raw target, so a 15 A cap is binding
    plant = await plant_factory(base_w=100.0, setpoint=14)
    await set_current_cap(hass, 15)
    await plant.tick(freezer, 12)
    assert plant.setpoints[-1] == 15, plant.setpoints
    assert set(plant.setpoints[-8:]) == {15}, plant.setpoints


async def test_decrease_of_one_amp_is_applied(
    hass: HomeAssistant, freezer, plant_factory
):
    """A computed target 1 A below the setpoint is applied without a deadband."""
    plant = await plant_factory(base_w=1000.0, setpoint=15)
    await plant.tick(freezer, 12)
    assert plant.setpoints[-1] == 14, plant.setpoints


async def test_settle_time_blocks_immediate_second_change(
    hass: HomeAssistant, freezer, plant_factory
):
    """No second setpoint change is issued within the settle time of the first."""
    plant = await plant_factory(base_w=1000.0, setpoint=9)
    await set_current_cap(hass, 9)
    await plant.tick(freezer, 8)
    await set_current_cap(hass, 16)
    plant.setpoints.clear()
    await plant.tick(freezer, 1)
    assert plant.setpoints == [14]

    # Make a large load step right after the change: the drop must wait for the settle time
    plant.base_w += 5000.0
    await plant.tick(freezer, 5)
    assert set(plant.setpoints) == {14}, plant.setpoints
    await plant.tick(freezer, 4)
    assert plant.setpoints[-1] < 14, plant.setpoints


async def test_transient_over_limit_does_not_stop_charging(
    hass: HomeAssistant, freezer, plant_factory
):
    """A single over-limit sample of the 15-min average is ignored."""
    plant = await plant_factory(base_w=1000.0, setpoint=14)
    # Register after setup so the integration's own switch platform does not replace it
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")
    await plant.tick(freezer, 4)

    plant.avg_import_15min = MAX_IMPORT + 400
    await plant.tick(freezer, 1)
    plant.avg_import_15min = 8000.0
    await plant.tick(freezer, 4)

    assert not turn_off_calls


async def test_sustained_over_limit_stops_charging(
    hass: HomeAssistant, freezer, plant_factory
):
    """An over-limit 15-min average that persists across ticks stops charging."""
    plant = await plant_factory(base_w=1000.0, setpoint=14)
    # Register after setup so the integration's own switch platform does not replace it
    turn_off_calls = async_mock_service(hass, "switch", "turn_off")
    await plant.tick(freezer, 4)

    plant.avg_import_15min = MAX_IMPORT + 400
    await plant.tick(freezer, 1)
    assert not turn_off_calls
    await plant.tick(freezer, 1)
    # Both sensors run the control loop and each may issue the call in the same tick
    assert turn_off_calls
    assert all(call.data["entity_id"] == CHARGER_SWITCH for call in turn_off_calls)
