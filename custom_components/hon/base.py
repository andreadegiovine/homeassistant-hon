import logging
import re
from typing import Any
from datetime import timedelta

from homeassistant.helpers.update_coordinator import ( DataUpdateCoordinator, CoordinatorEntity )
from homeassistant.core import callback
from homeassistant.components.select import SelectEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.components.binary_sensor import BinarySensorEntity

from .const import DOMAIN, APPLIANCE_DEFAULT_NAME

_LOGGER = logging.getLogger(__name__)

class HonBaseCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, hon, appliance):
        """Initialize my coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=hon._entry,
            name="hOn Device"
        )
        self._hon       = hon
        self._device    = None
        self._appliance = appliance

        try:
            self._mac           = appliance["macAddress"]
            self._type_name     = appliance["applianceTypeName"]
            self._type_id       = appliance["applianceTypeId"]
            self._name          = appliance.get("nickName", APPLIANCE_DEFAULT_NAME.get(str(self._type_id), "Device ID: " + str(self._type_id)))
            self._brand         = appliance["brand"]
            self._model         = appliance["modelName"]
            self._fw_version    = appliance["fwVersion"]
        except:
            _LOGGER.warning(f"Invalid appliance data in {appliance}" )


    async def _async_update_data(self):
        await self._device.get_status()
        await self._device.get_programs()
    #     _LOGGER.info("Remote new context")
    #     await self._hon.get_context(self._device)
    #     _LOGGER.info("Stored context")
    #     _LOGGER.info(self._device._attributes)

    @property
    def device(self):
        return self._device

    @device.setter
    def device(self, value):
        self._device = value

    @property
    def device_info(self):
        return {
            "identifiers": {
                (DOMAIN, self._mac, self._type_name)
            },
            "name": self._name,
            "manufacturer": self._brand,
            "model": self._model,
            "sw_version": self._fw_version,
        }

class HonBaseEntity(CoordinatorEntity):
    def __init__(self, coordinator, appliance, description):
        super().__init__(coordinator)

        """Hon properties"""
        self._coordinator           = coordinator
        self._mac                   = appliance["macAddress"]
        self._type_id               = appliance["applianceTypeId"]
        self._name                  = appliance.get("nickName", APPLIANCE_DEFAULT_NAME.get(str(self._type_id), "Device ID: " + str(self._type_id)))
        self._brand                 = appliance["brand"]
        self._model                 = appliance["modelName"]
        self._fw_version            = appliance["fwVersion"]
        self._type_name             = appliance["applianceTypeName"]
        self._sensor_name           = description.name
        self._key                   = description.key
        self._device                = coordinator.device
        self._translations          = None

        """Homeassistant properties"""
        key_formatted = re.sub(r'(?<!^)(?=[A-Z])', '_', self._key).lower()
        if( len(key_formatted) <= 0 ):
            key_formatted = re.sub(r'(?<!^)(?=[A-Z])', '_', self._sensor_name).lower()

        self.entity_description     = description
        self._attr_translation_key  = description.translation_key
        self._attr_has_entity_name  = True
        self._attr_unique_id        = self._mac + "_" + key_formatted

    @property
    def device_info(self):
        return {
            "identifiers": {
                (DOMAIN, self._mac, self._type_name)
            },
            "name": self._name,
            "manufacturer": self._brand,
            "model": self._model,
            "sw_version": self._fw_version,
        }

    @property
    def available(self) -> bool:
        """Entity is available"""
        return True


"""Used for update device settings"""
class HonBaseSensor(HonBaseEntity):
    def __init__(self, coordinator, appliance, description):
        super().__init__(coordinator, appliance, description)

        """First update"""
        self.coordinator_update()

    @property
    def available(self) -> bool:
        return self._device.get_current_program_param(self.entity_description.key) != None and "type" in self._device.get_current_program_param(self.entity_description.key) and self._device.is_on and (not self._device.is_running)

    @callback
    def _handle_coordinator_update(self):
        if self._coordinator.data is False:
            return
        try:
            self.coordinator_update()
            self.async_write_ha_state()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error updating %s", self.entity_id)

    def coordinator_update(self):
        """Update entity state"""
        raise NotImplementedError


class HonBaseSwitch(HonBaseSensor, SwitchEntity):
    @property
    def is_on(self):
        return self.available and self._device.get_current_program_param(self.entity_description.key)["value"] == 1

    async def async_turn_on(self, **kwargs: Any):
        self._device.set_current_program_param(self.entity_description.key, 1)

    async def async_turn_off(self, **kwargs: Any):
        self._device.set_current_program_param(self.entity_description.key, 0)

    def coordinator_update(self):
        if not self.available:
            self._attr_is_on = False
        else:
            self._attr_is_on = self._device.get_current_program_param(self.entity_description.key)["value"] == 1


class HonBaseSelect(HonBaseSensor, SelectEntity):
    @property
    def available(self) -> bool:
        return super().available and "options" in self._device.get_current_program_param(self.entity_description.key) and "type" in self._device.get_current_program_param(self.entity_description.key) and len(self._device.get_current_program_param(self.entity_description.key)["options"]) > 0

    @property
    def current_option(self) -> str | None:
        if not self.available:
            return None
        return str(self._device.get_current_program_param(self.entity_description.key)["value"])

    async def async_select_option(self, option: str) -> None:
        self._device.set_current_program_param(self.entity_description.key, str(option))

    def coordinator_update(self):
        if not self.available:
            self._attr_options = []
            self._attr_current_option = None
        else:
            self._attr_options = self._device.get_current_program_param(self.entity_description.key)["options"]
            self._attr_current_option = str(self._device.get_current_program_param(self.entity_description.key)["value"])


class HonBaseBinarySensor(HonBaseEntity, BinarySensorEntity):
    def __init__(self, coordinator, appliance, description, translations):
        super().__init__(coordinator, appliance, description)
        self._translations = translations

        """First update"""
        self.coordinator_update()

    @callback
    def _handle_coordinator_update(self):
        if self._coordinator.data is False:
            return
        try:
            self.coordinator_update()
            self.async_write_ha_state()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error updating %s", self.entity_id)

    def coordinator_update(self):
        """Update entity state"""
        self._attr_is_on = False
        if self._device.get_data(self.entity_description.key) is not None:
            self._attr_is_on = self._device.get_data(self.entity_description.key) == "1"