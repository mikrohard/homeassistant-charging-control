# Home Assistant Charging Control

Home Assistant integration for smart EV charging that dynamically adjusts charging current based on available power capacity to prevent overloading your electrical system.

## Features

- **Dynamic Load Balancing**: Automatically adjusts EV charging current based on real-time household power consumption
- **Three-Phase Support**: Monitors and manages power across all three phases (L1, L2, L3)
- **Overload Protection**: Prevents exceeding your maximum import power limit by reducing or stopping charging when necessary
- **Smart Power Calculation**: Uses both 30-second and 15-minute power averages for responsive yet stable control
- **Automatic Charger Control**: Can automatically control compatible EV charger switches and current settings
- **Configurable Update Interval**: Adjust how frequently the system checks and updates charging parameters

## Installation

### HACS (Recommended)

1. Open HACS in your Home Assistant instance
2. Click on "Integrations"
3. Click the three dots menu and select "Custom repositories"
4. Add this repository URL: `https://github.com/mikrohard/homeassistant-charging-control`
5. Select "Integration" as the category
6. Click "Add"
7. Search for "Charging Control" and install it
8. Restart Home Assistant

### Manual Installation

1. Copy the `custom_components/charging_control` folder to your Home Assistant `custom_components` directory
2. Restart Home Assistant

## Configuration

### Step 1: Add the Integration

1. Go to Settings → Devices & Services
2. Click "Add Integration"
3. Search for "Charging Control"
4. Follow the configuration wizard

### Step 2: Required Entities

You must provide the following sensor entities during setup:

- **Max Import Power Entity** (required): Sensor showing your maximum allowed import power in Watts
- **Average Import Power 15min Entity** (required): Sensor showing 15-minute average import power in Watts
- **Current L1 Entity** (required): Sensor showing current on phase L1 in Amperes
- **Voltage L1 Entity** (required): Sensor showing voltage on phase L1 in Volts

### Step 3: Optional Entities

For three-phase installations and charger monitoring:

- **Current L2/L3 Entity**: Sensors for current on phases L2 and L3
- **Voltage L2/L3 Entity**: Sensors for voltage on phases L2 and L3
- **Charger Current L1/L2/L3 Entity**: Sensors showing EV charger current consumption per phase

### Step 4: Charger Control (Optional)

To enable automatic charger control:

- **Charger Switch Entity**: Switch entity to turn the charger on/off
- **Charger Current Select Entity**: Select/input_select entity to set charging current (must have options like "6", "10", "16", "25", "32")

### Step 5: Other Settings

- **Update Interval**: How often to check and update charging parameters (5-60 seconds, default: 10)

## Entities Created

The integration creates the following entities:

### Sensors

- **sensor.charging_control_charging_allowed**: Binary indication if charging should be allowed
- **sensor.charging_control_max_charging_current**: Maximum safe charging current in Amperes

### Switches

- **switch.charging_control_charger_enabled**: Enable/disable automatic charger control
- **switch.charging_control_manual_override**: Override automatic control for manual operation

### Select

- **select.charging_control_max_current_cap**: Set maximum charging current limit (6-32A)

## Services

### charging_control.update_charger

Manually trigger an update of the charger settings based on current calculations.

```yaml
service: charging_control.update_charger
data:
  entry_id: "optional_config_entry_id"
```

## How It Works

1. **Power Monitoring**: The integration continuously monitors your household power consumption across all phases
2. **Available Power Calculation**: Calculates available power by subtracting current usage from your maximum import limit
3. **Safety Checks**:
   - Ensures 15-minute average power stays below maximum (grid protection)
   - Checks if there's enough power for minimum charging (6A)
   - Monitors phase currents to prevent overload
4. **Dynamic Adjustment**: Adjusts charging current in real-time based on available capacity
5. **Automatic Control**: If configured, automatically controls your EV charger switch and current settings

## Safety Features

- **Minimum Current**: Never sets charging below 6A (if insufficient power, charging stops instead)
- **Maximum Current Cap**: User-configurable maximum limit (default 32A)
- **15-Minute Average Protection**: Prevents grid connection overload penalties
- **Phase Balancing**: Assumes balanced three-phase charging for calculations
- **Fail-Safe**: If any calculation fails, charging is disabled for safety

## Example Automation

Create automations based on the charging control sensors:

```yaml
automation:
  - alias: "Notify when charging stops due to overload"
    trigger:
      - platform: state
        entity_id: sensor.charging_control_charging_allowed
        from: "True"
        to: "False"
    action:
      - service: notify.mobile_app
        data:
          message: "EV charging stopped due to high power consumption"

  - alias: "Update charger every minute"
    trigger:
      - platform: time_pattern
        minutes: "/1"
    action:
      - service: charging_control.update_charger
```

## Troubleshooting

### Charging doesn't start
- Check that all required entities are providing valid data
- Verify your maximum import power setting
- Ensure there's enough available power for minimum 6A charging

### Charging current too low
- Check current household consumption
- Verify the 15-minute average isn't approaching the limit
- Adjust the maximum current cap if needed

### Charger not responding
- Verify the charger switch and select entities are correctly configured
- Check that the select entity has appropriate current options (6, 10, 16, 25, 32, etc.)
- Enable debug logging to see control commands

### Debug Logging

Add to your `configuration.yaml`:

```yaml
logger:
  default: info
  logs:
    custom_components.charging_control: debug
```

## Support

For issues and feature requests, please use the [GitHub issue tracker](https://github.com/mikrohard/homeassistant-charging-control/issues).

## License

This project is licensed under the MIT License.