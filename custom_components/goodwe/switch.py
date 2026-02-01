"""GoodWe PV inverter switch entities."""

from dataclasses import dataclass
import logging
from typing import Any

from goodwe import Inverter, InverterError
from homeassistant.components.switch import (
    SwitchDeviceClass,
    SwitchEntity,
    SwitchEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import BaseCoordinatorEntity

from .const import (
    DOMAIN,
    OBSERVATION_33XXX,
    OBSERVATION_38XXX,
    OBSERVATION_48XXX,
    OBSERVATION_55XXX,
)
from .coordinator import GoodweUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class GoodweSwitchEntityDescription(SwitchEntityDescription):
    """Class describing Goodwe switch entities."""

    setting: str
    polling_interval: int = 0


@dataclass(frozen=True, kw_only=True)
class ObservationSwitchEntityDescription(SwitchEntityDescription):
    """Class describing observation sensor switch entities."""

    attribute: str  # The _observe_*xxx attribute name


SWITCHES = (
    GoodweSwitchEntityDescription(
        key="load_control",
        translation_key="load_control",
        device_class=SwitchDeviceClass.OUTLET,
        setting="load_control_switch",
    ),
    GoodweSwitchEntityDescription(
        key="grid_export_limit_switch",
        translation_key="grid_export_limit_switch",
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        setting="grid_export",
    ),
    GoodweSwitchEntityDescription(
        key="fast_charging_switch",
        translation_key="fast_charging_switch",
        device_class=SwitchDeviceClass.SWITCH,
        setting="fast_charging",
        polling_interval=30,
    ),
    GoodweSwitchEntityDescription(
        key="backup_supply_switch",
        translation_key="backup_supply_switch",
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        setting="backup_supply",
    ),
    GoodweSwitchEntityDescription(
        key="fixed_q_power_flag",
        translation_key="fixed_q_power_flag",
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        setting="fixed_q_power_flag",
    ),
    GoodweSwitchEntityDescription(
        key="fixed_power_factor_enable",
        translation_key="fixed_power_factor_enable",
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        setting="fixed_power_factor_enable",
    ),
    GoodweSwitchEntityDescription(
        key="dod_holding_switch",
        translation_key="dod_holding_switch",
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        setting="dod_holding",
    ),
    GoodweSwitchEntityDescription(
        key="peak_shaving_enabled",
        translation_key="peak_shaving_enabled",
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        setting="peak_shaving_enabled",
        polling_interval=30,
    ),
)

OBSERVATION_SWITCHES = (
    ObservationSwitchEntityDescription(
        key=OBSERVATION_33XXX,
        translation_key=OBSERVATION_33XXX,
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        attribute="_observe_33xxx",
    ),
    ObservationSwitchEntityDescription(
        key=OBSERVATION_38XXX,
        translation_key=OBSERVATION_38XXX,
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        attribute="_observe_38xxx",
    ),
    ObservationSwitchEntityDescription(
        key=OBSERVATION_48XXX,
        translation_key=OBSERVATION_48XXX,
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        attribute="_observe_48xxx",
    ),
    ObservationSwitchEntityDescription(
        key=OBSERVATION_55XXX,
        translation_key=OBSERVATION_55XXX,
        entity_category=EntityCategory.CONFIG,
        device_class=SwitchDeviceClass.SWITCH,
        attribute="_observe_55xxx",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the inverter switch entities from a config entry."""
    inverter = config_entry.runtime_data.inverter
    coordinator = config_entry.runtime_data.coordinator
    device_info = config_entry.runtime_data.device_info

    entities = []

    for description in SWITCHES:
        try:
            current_state = await inverter.read_setting(description.setting)
        except (InverterError, ValueError):
            # Inverter model does not support this feature
            _LOGGER.debug("Could not read %s value", description.setting)
        else:
            entities.append(
                InverterSwitchEntity(
                    coordinator,
                    device_info,
                    description,
                    inverter,
                    current_state == 1,
                )
            )

    # Add observation switches (always available, no read check needed)
    for description in OBSERVATION_SWITCHES:
        current_state = getattr(inverter, description.attribute, False)
        entities.append(
            ObservationSwitchEntity(
                coordinator,
                device_info,
                description,
                inverter,
                current_state,
            )
        )

    async_add_entities(entities)


class InverterSwitchEntity(
    BaseCoordinatorEntity[GoodweUpdateCoordinator], SwitchEntity
):
    """Switch representation of inverter's 'Load Control' relay."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    entity_description: GoodweSwitchEntityDescription

    def __init__(
        self,
        coordinator: GoodweUpdateCoordinator,
        device_info: DeviceInfo,
        description: GoodweSwitchEntityDescription,
        inverter: Inverter,
        current_is_on: bool,
    ) -> None:
        """Initialize the inverter operation mode setting entity."""
        super().__init__(coordinator)
        self.entity_description = description
        # Use sensor_name_prefix (GWxxxx_) to distinguish parallel inverters
        prefix = inverter.sensor_name_prefix if hasattr(inverter, 'sensor_name_prefix') else ""
        self._attr_unique_id = f"{DOMAIN}-{prefix}{description.key}-{inverter.serial_number}"
        self._attr_device_info = device_info
        self._attr_is_on = current_is_on
        self._inverter: Inverter = inverter
        self._notify_coordinator()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._inverter.write_setting(self.entity_description.setting, 1)
        self._attr_is_on = True
        self._notify_coordinator()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._inverter.write_setting(self.entity_description.setting, 0)
        self._attr_is_on = False
        self._notify_coordinator()
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Get the current value from inverter."""
        value = await self._inverter.read_setting(self.entity_description.setting)
        self._attr_is_on = value == 1
        self._notify_coordinator()

    def _notify_coordinator(self) -> None:
        if self.entity_description.polling_interval:
            self.coordinator.entity_state_polling(
                self,
                self.entity_description.polling_interval if self._attr_is_on else 0,
            )


class ObservationSwitchEntity(
    BaseCoordinatorEntity[GoodweUpdateCoordinator], SwitchEntity
):
    """Switch to enable/disable observation sensors for undocumented registers."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    entity_description: ObservationSwitchEntityDescription

    def __init__(
        self,
        coordinator: GoodweUpdateCoordinator,
        device_info: DeviceInfo,
        description: ObservationSwitchEntityDescription,
        inverter: Inverter,
        current_is_on: bool,
    ) -> None:
        """Initialize the observation switch entity."""
        super().__init__(coordinator)
        self.entity_description = description
        # Use sensor_name_prefix (GWxxxx_) to distinguish parallel inverters
        prefix = inverter.sensor_name_prefix if hasattr(inverter, 'sensor_name_prefix') else ""
        self._attr_unique_id = f"{DOMAIN}-{prefix}{description.key}-{inverter.serial_number}"
        self._attr_device_info = device_info
        self._attr_is_on = current_is_on
        self._inverter: Inverter = inverter

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the observation sensors on."""
        setattr(self._inverter, self.entity_description.attribute, True)
        self._attr_is_on = True
        self.async_write_ha_state()
        _LOGGER.info(
            "Enabled observation sensors: %s",
            self.entity_description.attribute
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the observation sensors off."""
        setattr(self._inverter, self.entity_description.attribute, False)
        self._attr_is_on = False
        self.async_write_ha_state()
        _LOGGER.info(
            "Disabled observation sensors: %s",
            self.entity_description.attribute
        )

    async def async_update(self) -> None:
        """Get the current state from inverter attribute."""
        self._attr_is_on = getattr(
            self._inverter,
            self.entity_description.attribute,
            False
        )
