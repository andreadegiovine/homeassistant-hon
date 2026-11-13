import logging
from datetime import timedelta

from homeassistant.components.binary_sensor import BinarySensorEntityDescription
from homeassistant.helpers import translation

from .const import DOMAIN, SENSORS_DEFAULT
from .base import HonBaseBinarySensor
from .utils import get_datetime, to_int

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities) -> None:
    hon = hass.data[DOMAIN][entry.unique_id]
    appliances = []

    translations = await translation.async_get_translations(hass, hass.config.language, "entity")

    for appliance in hon.appliances:

        coordinator = hon.async_get_coordinator(appliance)

        default_value = SENSORS_DEFAULT.get(coordinator.device._type_name.lower(), {})

        description = BinarySensorEntityDescription(
            key=coordinator.device._type_name.lower(),
            name=None,
            translation_key = "device",
            icon=default_value.get("icon", None),
        )
        appliances.extend([HonDevice(coordinator, appliance, description, translations)])

        sensors = default_value.get("binary_sensors", {})
        for key in sensors:
            sensor_default = sensors.get(key, {})
            description = BinarySensorEntityDescription(
                key=key,
                name=key,
                translation_key=key,
                icon=sensor_default.get("icon", None),
            )
            appliances.extend([HonBaseBinarySensor(coordinator, appliance, description, translations)])

    async_add_entities(appliances)


class HonDevice(HonBaseBinarySensor):
    def get_program_duration(self):
        program_data = self._device.get_program(self._device.current_program_name)
        if not program_data or "timing" not in program_data:
            return None
        timing = 0
        timing_data = program_data["timing"]
        for option in timing_data:
            if self._device.get_current_program_param(option) and "value" in self._device.get_current_program_param(option):
                option_setting = self._device.get_current_program_param(option)["value"]
                option_setting = str(option_setting)
                if option in ["dirtyLevel","dryLevel","dryTime"]:
                    if option_setting in timing_data[option]:
                        timing = timing + int(timing_data[option][option_setting])
                    else:
                        timing = timing + int(timing_data[option]["default"])
                elif option == "steamLevel":
                    timing = timing + (to_int(timing_data["steamLevel"].get("+steamType", {}).get(option_setting)) or 0)
                else:
                    for phase in timing_data[option]:
                        if option_setting in timing_data[option][phase]:
                            timing = timing + int(timing_data[option][phase][option_setting])
                        else:
                            _LOGGER.error("Missing timing value")
                            _LOGGER.error(option)
                            _LOGGER.error(option_setting)
                            _LOGGER.error(timing_data[option])
        if timing//60 > 0:
            hours = timing//60
            minutes = timing%60
            if minutes < 10:
                minutes = "0" + str(minutes)
            timing = str(hours) + ":" + str(minutes)
        else:
            timing = str(timing) + " min"
        return timing

    def coordinator_update(self):
        self._attr_is_on = self._device.is_on

        attributes = {"mac": self._device._mac}

        if self._attr_is_on == False:
            self._attr_extra_state_attributes = attributes
            return

        if self._device.get_data("machMode"):
            mode = self._device.get_data("machMode")
            if mode == "5":
                mode = "4"
            attributes["mode"] = mode

        for key in ["error","errors"]:
            if self._device.get_data(key) and self._device.get_data(key) != "00":
                attributes["error"] = self._device.get_data(key)

        if self._device.is_running:
            if self._device.get_data("temp"):
                attributes["temperature"] = str(self._device.get_data("temp")) + " °C"

            if self._device.get_data("dryLevel"):
                dry_level = self._device.get_data("dryLevel")
                translation_path = f"component.hon.entity.select.drylevel.state.{dry_level}"
                attributes["dry_level"] = self._translations.get(translation_path, dry_level)

            if self._device.get_data("prPhase"):
                pr_phase = self._device.get_data("prPhase")
                translation_path = f"component.hon.entity.binary_sensor.{self._coordinator.device._type_name.lower()}.state_attributes.program_phase.state.{pr_phase}"
                attributes["program_phase"] = self._translations.get(translation_path, pr_phase)

            if self._device.get_data("spinSpeed"):
                attributes["spin_speed"] = str(self._device.get_data("spinSpeed")) + " rpm"


            remaining = to_int(self._device.get_data("remainingTimeMM"))
            if remaining is not None:
                delay = to_int(self._device.get_data("delayTime"), 0)
                remaining_text = None
                end_time = None
                if remaining > 0:
                    end_time = (get_datetime().replace(second=0, microsecond=0) + timedelta(minutes=delay + remaining)).strftime("%H:%M")
                    if remaining // 60 > 0:
                        remaining_text = f"{remaining // 60}:{remaining % 60:02d}"
                    else:
                        remaining_text = f"{remaining} min"

                attributes["remaining_time"] = remaining_text
                attributes["end_time"] = end_time

        if self._device.current_program_name:
            translation_path = f"component.hon.entity.select.{self._coordinator.device._type_name.lower()}_program.state.{self._device.current_program_name}"
            attributes["program_name"] = self._translations.get(translation_path, self._device.current_program_name)

            attributes["program_description"] = (self._device.get_program(self._device.current_program_name) or {}).get("info")
            attributes["program_duration"] = self.get_program_duration()

            params = self._device.current_program_settings
            for param in params:
                attributes["program_"+param] = params[param]

        self._attr_extra_state_attributes = attributes
