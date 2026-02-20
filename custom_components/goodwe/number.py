"""GoodWe PV inverter numeric settings entities."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import logging

from goodwe import Inverter, InverterError
from homeassistant.components.number import (
    NumberDeviceClass,
    NumberEntity,
    NumberEntityDescription,
)
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfElectricCurrent, UnitOfPower
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import GoodweConfigEntry

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GoodweNumberEntityDescription(NumberEntityDescription):
    """Class describing Goodwe number entities."""

    getter: Callable[[Inverter], Awaitable[any]]
    mapper: Callable[[any], int]
    setter: Callable[[Inverter, int], Awaitable[None]]
    filter: Callable[[Inverter], bool]


def _get_setting_unit(inverter: Inverter, setting: str) -> str:
    """Return the unit of an inverter setting."""
    return next((s.unit for s in inverter.settings() if s.id_ == setting), "")


async def set_offline_battery_dod(inverter: Inverter, dod: int) -> None:
    """Sets offline battery dod - dod for backup output"""
    if 10 <= dod <= 100:
        await inverter.write_setting('battery_discharge_depth_offline', 100 - dod)


async def get_offline_battery_dod(inverter: Inverter) -> int:
    """Returns offline battery dod - dod for backup output"""
    return 100 - (await inverter.read_setting('battery_discharge_depth_offline'))

NUMBERS = (
    # Only one of the export limits are added.
    # Availability is checked in the filter method.
    # Export limit in W
    GoodweNumberEntityDescription(
        key="grid_export_limit",
        translation_key="grid_export_limit",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_step=100,
        native_min_value=0,
        getter=lambda inv: inv.get_grid_export_limit(),
        mapper=lambda v: v,
        setter=lambda inv, val: inv.set_grid_export_limit(val),
        filter=lambda inv: _get_setting_unit(inv, "grid_export_limit") != "%",
    ),
    # Export limit in %
    GoodweNumberEntityDescription(
        key="grid_export_limit",
        translation_key="grid_export_limit",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=200,
        getter=lambda inv: inv.get_grid_export_limit(),
        mapper=lambda v: v,
        setter=lambda inv, val: inv.set_grid_export_limit(val),
        filter=lambda inv: _get_setting_unit(inv, "grid_export_limit") == "%",
    ),
    GoodweNumberEntityDescription(
        key="battery_discharge_depth",
        translation_key="battery_discharge_depth",
        icon="mdi:battery-arrow-down",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=99,
        getter=lambda inv: inv.get_ongrid_battery_dod(),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.set_ongrid_battery_dod(int(val)),
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="battery_discharge_depth_offline",
        translation_key="battery_discharge_depth_offline",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=99,
        getter=lambda inv: get_offline_battery_dod(inv),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: set_offline_battery_dod(inv, int(val)),
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="eco_mode_power",
        translation_key="eco_mode_power",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=100,
        getter=lambda inv: inv.read_setting("eco_mode_1"),
        mapper=lambda v: abs(v.get_power()) if v.get_power() else 0,
        setter=None,
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="eco_mode_soc",
        translation_key="eco_mode_soc",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=100,
        getter=lambda inv: inv.read_setting("eco_mode_1"),
        mapper=lambda v: v.soc if v.soc else 0,
        setter=None,
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="fast_charging_power",
        translation_key="fast_charging_power",
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=100,
        getter=lambda inv: inv.read_setting("fast_charging_power"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("fast_charging_power", int(val)),
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="fast_charging_soc",
        translation_key="fast_charging_soc",
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=100,
        getter=lambda inv: inv.read_setting("fast_charging_soc"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("fast_charging_soc", int(val)),
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="fixed_reactive_power",
        translation_key="fixed_reactive_power",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement="‰",
        native_step=10,
        native_min_value=-600,
        native_max_value=600,
        getter=lambda inv: inv.read_setting("fixed_reactive_power"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("fixed_reactive_power", int(val)),
        filter=lambda inv: True,
    ),
    # PCS Powersave Mode (ARM fw >= 19)
    GoodweNumberEntityDescription(
        key="pcs_powersave_mode",
        translation_key="pcs_powersave_mode",
        entity_category=EntityCategory.CONFIG,
        native_step=1,
        native_min_value=0,
        native_max_value=65535,
        getter=lambda inv: inv.read_setting("pcs_powersave_mode"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("pcs_powersave_mode", int(val)),
        filter=lambda inv: True,
    ),
    # Battery current limits (registers 45353, 45355)
    GoodweNumberEntityDescription(
        key="battery_charge_current",
        translation_key="battery_charge_current",
        icon="mdi:battery-charging",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_step=1,
        native_min_value=0,
        native_max_value=200,
        getter=lambda inv: inv.read_setting("battery_charge_current"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("battery_charge_current", int(val)),
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="battery_discharge_current",
        translation_key="battery_discharge_current",
        icon="mdi:battery-arrow-down-outline",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        native_step=1,
        native_min_value=0,
        native_max_value=200,
        getter=lambda inv: inv.read_setting("battery_discharge_current"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("battery_discharge_current", int(val)),
        filter=lambda inv: True,
    ),
    # Peak Shaving parameters (TOU Slot 8 when in peak shaving mode 0xFC)
    # Note: In parallel systems, this value is sent to EACH inverter separately,
    # so total system power limit = value * number_of_inverters
    # Register encoding: value stored in 10W units (register 3800 = 38000W = 38kW)
    GoodweNumberEntityDescription(
        key="peak_shaving_power_slot8",
        translation_key="peak_shaving_power_slot8",
        icon="mdi:transmission-tower-export",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_step=10,
        native_min_value=-40000,
        native_max_value=40000,
        getter=lambda inv: inv.read_setting("tou_slot8_param1"),
        mapper=lambda v: v * 10 if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("tou_slot8_param1", int(val / 10)),
        filter=lambda inv: True,
    ),
    GoodweNumberEntityDescription(
        key="peak_shaving_soc_slot8",
        translation_key="peak_shaving_soc_slot8",
        icon="mdi:battery-charging-70",
        entity_category=EntityCategory.CONFIG,
        native_unit_of_measurement=PERCENTAGE,
        native_step=1,
        native_min_value=0,
        native_max_value=100,
        getter=lambda inv: inv.read_setting("tou_slot8_param2"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("tou_slot8_param2", int(val)),
        filter=lambda inv: True,
    ),
    # Battery FeedPower Offset (ARM fw >= 19) - offset applied to battery feed power in W
    GoodweNumberEntityDescription(
        key="bat_feedpower_offset",
        translation_key="bat_feedpower_offset",
        icon="mdi:battery-arrow-up",
        entity_category=EntityCategory.CONFIG,
        device_class=NumberDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        native_step=1,
        native_min_value=0,
        native_max_value=10000,
        getter=lambda inv: inv.read_setting("bat_feedpower_offset"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("bat_feedpower_offset", int(val)),
        filter=lambda inv: True,
    ),
    # AC Active Current Limit Flag (2025 firmware) - limit AC active current
    # Special values: 0xFFFF=DSP PV power limit, 0xFFFE=DSP PV power percentage limit
    GoodweNumberEntityDescription(
        key="ac_active_limit_flag",
        translation_key="ac_active_limit_flag",
        icon="mdi:current-ac",
        entity_category=EntityCategory.CONFIG,
        native_step=1,
        native_min_value=0,
        native_max_value=1000,
        getter=lambda inv: inv.read_setting("ac_active_limit_flag"),
        mapper=lambda v: v if v is not None else 0,
        setter=lambda inv, val: inv.write_setting("ac_active_limit_flag", int(val)),
        filter=lambda inv: True,
    ),
    # TOU (Time of Use) Slot Parameters (ARM fw >= 19 for slots 1-4, >= 22 for 5-8)
    # Param1 and Param2 for each slot (meaning depends on work week mode)
)

# Add TOU slot parameters dynamically
_TOU_NUMBERS = []
for slot in range(1, 9):
    _TOU_NUMBERS.extend([
        GoodweNumberEntityDescription(
            key=f"tou_slot{slot}_param1",
            translation_key=f"tou_slot{slot}_param1",
            entity_category=EntityCategory.CONFIG,
            native_step=1,
            native_min_value=0,
            native_max_value=65535,
            getter=lambda inv, s=slot: inv.read_setting(f"tou_slot{s}_param1"),
            mapper=lambda v: v if v is not None else 0,
            setter=lambda inv, val, s=slot: inv.write_setting(f"tou_slot{s}_param1", int(val)),
            filter=lambda inv: True,
        ),
        GoodweNumberEntityDescription(
            key=f"tou_slot{slot}_param2",
            translation_key=f"tou_slot{slot}_param2",
            entity_category=EntityCategory.CONFIG,
            native_step=1,
            native_min_value=0,
            native_max_value=65535,
            getter=lambda inv, s=slot: inv.read_setting(f"tou_slot{s}_param2"),
            mapper=lambda v: v if v is not None else 0,
            setter=lambda inv, val, s=slot: inv.write_setting(f"tou_slot{s}_param2", int(val)),
            filter=lambda inv: True,
        ),
    ])

NUMBERS = NUMBERS + tuple(_TOU_NUMBERS)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: GoodweConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up the inverter select entities from a config entry."""
    inverter = config_entry.runtime_data.inverter
    device_info = config_entry.runtime_data.device_info

    entities = []

    for description in filter(lambda dsc: dsc.filter(inverter), NUMBERS):
        try:
            current_value = description.mapper(await description.getter(inverter))
        except (InverterError, ValueError):
            # Inverter model does not support this setting
            _LOGGER.debug("Could not read inverter setting %s", description.key)
            continue

        entity = InverterNumberEntity(device_info, description, inverter, current_value)
        # Set the max value of grid_export_limit (W version)
        if (
            description.key == "grid_export_limit"
            and description.native_unit_of_measurement == UnitOfPower.WATT
        ):
            entity.native_max_value = (
                inverter.rated_power * 2 if inverter.rated_power else 10000
            )
        entities.append(entity)

    async_add_entities(entities)


class InverterNumberEntity(NumberEntity):
    """Inverter numeric setting entity."""

    _attr_should_poll = True
    _attr_has_entity_name = True
    entity_description: GoodweNumberEntityDescription

    def __init__(
        self,
        device_info: DeviceInfo,
        description: GoodweNumberEntityDescription,
        inverter: Inverter,
        current_value: int,
    ) -> None:
        """Initialize the number inverter setting entity."""
        self.entity_description = description
        # Use sensor_name_prefix (GWxxxx_) to distinguish parallel inverters
        prefix = inverter.sensor_name_prefix if hasattr(inverter, 'sensor_name_prefix') else ""
        self._attr_unique_id = f"{DOMAIN}-{prefix}{description.key}-{inverter.serial_number}"
        self._attr_device_info = device_info
        self._attr_native_value = (
            float(current_value) if current_value is not None else None
        )

        self._inverter: Inverter = inverter

    async def async_update(self) -> None:
        """Get the current value from inverter."""
        try:
            value = await self.entity_description.getter(self._inverter)
            self._attr_native_value = float(self.entity_description.mapper(value))
        except (InverterError, ValueError):
            _LOGGER.debug("Failed to update number entity %s", self.entity_description.key)

    async def async_set_native_value(self, value: float) -> None:
        """Set new value to inverter."""
        if self.entity_description.setter:
            await self.entity_description.setter(self._inverter, int(value))
            # Read back to confirm actual value accepted by inverter
            try:
                actual = await self.entity_description.getter(self._inverter)
                self._attr_native_value = float(self.entity_description.mapper(actual))
            except (InverterError, ValueError):
                self._attr_native_value = value
        else:
            self._attr_native_value = value
        self.async_write_ha_state()
