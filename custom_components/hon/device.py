import logging
from copy import deepcopy
import os
import json

from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.notify import (
    ATTR_MESSAGE,
    ATTR_TITLE,
    SERVICE_PERSISTENT_NOTIFICATION,
)
from homeassistant.components.notify import DOMAIN as NOTIFICATION_DOMAIN
from deep_translator import GoogleTranslator

from .const import (
    DOMAIN,
    APPLIANCE_DEFAULT_NAME,
    CONF_MAC,
    CONF_SOFTENER_REMAINING_TIME,
    CONF_PROGRAMS,
    CONF_SETTINGS,
    CONF_CURRENT_PROGRAM,
    CONF_PROGRAMS_SETTINGS,
    CONF_GLOBAL_SETTINGS
)
from .utils import to_int

_LOGGER = logging.getLogger(__name__)

class HonDevice(CoordinatorEntity):
    def __init__(self, entry, hon, coordinator, appliance, translations) -> None:
        super().__init__(coordinator)

        self._translations  = translations
        self._entry         = entry
        self._hon           = hon
        self._coordinator   = coordinator
        self._hass          = self._coordinator.hass
        self._appliance     = appliance
        self._brand         = appliance["brand"]
        self._type_name     = appliance["applianceTypeName"]
        self._type_id       = appliance["applianceTypeId"]
        self._name          = appliance.get("nickName", APPLIANCE_DEFAULT_NAME.get(str(self._type_id), "Device ID: " + str(self._type_id)))
        self._mac           = appliance["macAddress"]
        self._model         = appliance["modelName"]
        self._series        = appliance["series"]
        self._model_id      = appliance["applianceModelId"]
        self._serial_number = appliance["serialNumber"]
        self._fw_version    = appliance["fwVersion"]
        self._mac_address   = appliance["macAddress"]

        self._attributes = {}
        self._delay_time = None
        self._manually_detergent_notify = False
        self._manually_softener_notify = False
        self._low_detergent_notify = False
        self._low_softener_notify = False

        self._cache = {}

    def get_yaml_config(self, key):
        yaml = self._hass.data[DOMAIN]["configuration_yaml"]
        if not yaml:
            return None
        for item in yaml:
            if item[CONF_MAC] == self._mac and key in item:
                return item[key]
        return None

    def set_storage_data(self, key: str, value):
        hass_config_path = self._hass.config.path()
        storage_path = os.path.join(hass_config_path, ".storage", DOMAIN)
        if not os.path.isdir(storage_path):
            os.mkdir(storage_path)
        data_path = os.path.join(storage_path, f"{self._mac}_{key}.json")
        tmp_path = data_path + ".tmp"
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=4)
        os.replace(tmp_path, data_path)
        self._cache[key] = value

    def get_storage_data(self, key: str):
        """Read from cache; the files are preloaded by async_load_storage (no I/O in the event loop)."""
        if key in self._cache:
            return self._cache[key]
        hass_config_path = self._hass.config.path()
        storage_path = os.path.join(hass_config_path, ".storage", DOMAIN)
        data_path = os.path.join(storage_path, f"{self._mac}_{key}.json")
        value = None
        if os.path.isdir(storage_path) and os.path.isfile(data_path):
            with open(data_path) as f:
                value = json.load(f)
        self._cache[key] = value
        return value

    async def async_load_storage(self):
        """Load programs and settings from disk in the executor."""
        for key in (CONF_PROGRAMS, CONF_SETTINGS):
            self._cache.pop(key, None)
            await self._hass.async_add_executor_job(self.get_storage_data, key)

    def copy_dict(self, data):
        new_data = {}
        for data_key in data:
            if isinstance(data[data_key], dict):
                new_data[data_key] = self.copy_dict(data[data_key])
            else:
                new_data[data_key] = deepcopy(data[data_key])
        return new_data

    def get_entry_data(self):
        return self.copy_dict(self._entry.data)

    def get_config_data(self, key):
        data = self.get_entry_data()
        if self._mac not in data or key not in data[self._mac]:
            return {}
        return data[self._mac][key]

    def set_stored_data(self, key, value):
        data = self.get_entry_data()
        if self._mac not in data:
            data[self._mac] = {}
        if key not in data[self._mac]:
            data[self._mac][key] = {}
        data[self._mac][key] = value
        self._hass.config_entries.async_update_entry(self._entry, data=data)
        # self._hass.config_entries._async_schedule_save()
        # self._coordinator.async_update_listeners()

    @property
    def settings(self):
        settings = self.get_storage_data(CONF_SETTINGS) or {}
        stored_settings = self.get_config_data(CONF_GLOBAL_SETTINGS)
        for setting in stored_settings:
            if setting in settings:
                settings[setting]["value"] = stored_settings[setting]
        return settings

    def get_setting(self, key):
        if key not in self.settings:
            return None
        return self.settings[key]

    def update_setting(self, key, value):
        if key not in self.settings:
            return
        data = self.get_config_data(CONF_GLOBAL_SETTINGS)
        data[key] = value
        self.set_stored_data(CONF_GLOBAL_SETTINGS, data)

    @property
    def programs(self):
        programs = self.get_storage_data(CONF_PROGRAMS) or {}
        stored_programs = self.get_config_data(CONF_PROGRAMS_SETTINGS)
        for program in stored_programs:
            for param in stored_programs[program]:
                if program in programs and param in programs[program]["params"]:
                    programs[program]["params"][param]["value"] = stored_programs[program][param]
        return programs

    def get_program(self, key):
        if not key:
            return None
        return self.programs.get(key)

    def get_program_params(self, key):
        data = self.get_program(key)
        if data == None:
            return None
        return data["params"]

    def update_program(self, key, value):
        if key not in self.programs:
            return
        data = self.get_config_data(CONF_PROGRAMS_SETTINGS)
        data[key] = value
        self.set_stored_data(CONF_PROGRAMS_SETTINGS, data)

    def update_program_params(self, name, key, value):
        if name not in self.programs or key not in self.programs[name]["params"]:
            return
        data = {}
        if name in self.get_config_data(CONF_PROGRAMS_SETTINGS):
            data = self.get_config_data(CONF_PROGRAMS_SETTINGS)[name]
        data[key] = value
        self.update_program(name, data)

    @property
    def current_program_name(self):
        result = None
        if self.get_config_data(CONF_CURRENT_PROGRAM):
            result = self.get_config_data(CONF_CURRENT_PROGRAM)
        return result

    def set_current_program(self, name):
        self.set_stored_data(CONF_CURRENT_PROGRAM, name)

    @property
    def current_program_params(self):
        if not self.current_program_name:
            return {}
        params = dict(self.get_program_params(self.current_program_name) or {})
        for param in params:
            if param in ["delayTime", "lang", "waterHard"]:
                setting = self.get_setting(param)
                if setting != None:
                    params[param] = setting
        return params

    def get_current_program_param(self, key):
        data = self.current_program_params
        if key not in data:
            return None
        return data[key]

    def set_current_program_param(self, key, value):
        if key in ["delayTime", "lang", "waterHard"]:
            self.update_setting(key, value)
        else:
            self.update_program_params(self.current_program_name, key, value)
        self._coordinator.async_update_listeners()

    @property
    def current_program_settings(self):
        settings = {}
        params = self.current_program_params
        for param in params:
            if "value" in params[param]:
                settings[param] = str(params[param]["value"])
        return settings

    @property
    def all_params(self):
        params = {}
        for program_name in self.programs:
            params[program_name] = self.programs[program_name]["params"]
        return params

    @property
    def sensors(self):
        sensors = {"switch": [], "select": []}
        for program_name in self.all_params:
            program_options = self.all_params[program_name]
            for option_name in program_options:
                option = program_options[option_name]
                if option.get("type") in sensors and option_name not in sensors[option["type"]]:
                    sensors[option["type"]].append(option_name)
        for setting in self.settings:
            option = self.settings[setting]
            if option.get("type") in sensors and setting not in sensors[option["type"]]:
                sensors[option["type"]].append(setting)
        return sensors

    @property
    def is_on(self):
        return self.get_data("remoteCtrValid") == "1" and self.get_data("lastConnEvent") == "CONNECTED"

    @property
    def is_available(self):
        return self.is_on

    @property
    def is_running(self):
        if self.get_data("machMode") in ["2","3","4","5"]:
            return True
        return False

    def get_data(self, key):
        if key in self._attributes:
            return self._attributes[key]
        return None

    def set_data(self, data):
        for key in data:
            self._attributes[key] = data[key]
        self._coordinator.async_update_listeners()

    async def send_notify(self, message):
        data = {ATTR_TITLE: self._name}
        data[ATTR_MESSAGE] = message
        await self._hass.services.async_call(NOTIFICATION_DOMAIN, SERVICE_PERSISTENT_NOTIFICATION, data)

    async def get_translation(self, string):
        try:
            translator = GoogleTranslator(source='auto', target='it')
            translated = await self._hass.async_add_executor_job(translator.translate, string)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Translation failed (%s), using original description", err)
            return string
        return translated or string

    def get_param_config(self, param, configs, stored_configs = None):
        result = {}
        default_value = None
        if stored_configs:
            default_value = int(stored_configs["value"])
        elif "defaultValue" in configs:
            default_value = int(configs["defaultValue"])
        if configs["typology"].strip() == "range" and ("maximumValue" in configs):
            if int(configs["minimumValue"]) == 0 and (int(configs["maximumValue"]) == 1 or int(configs["maximumValue"]) == int(configs["incrementValue"])):
                result = {"type": "switch", "value": default_value}
            else:
                options = list(range(int(configs["minimumValue"]), (int(configs["maximumValue"])+int(configs["incrementValue"])), int(configs["incrementValue"])))
                result = {"type": "select", "value": default_value, "options": list(map(str, options))}
        elif configs["typology"].strip() == "enum":
            result = {"type": "select", "value": default_value, "options": list(map(str, configs["enumValues"]))}
        elif "mandatory" in configs and "fixedValue" in configs:
            result = {"value": int(configs["fixedValue"])}
        if param == "delayTime":
            result["value"] = 0
        return result

    async def get_programs(self):
        programs = self.get_storage_data(CONF_PROGRAMS)
        settings = self.get_storage_data(CONF_SETTINGS)
        if not programs or not settings:
            commands = await self._hon.get_programs(self._appliance)
            if "startProgram" not in commands:
                return
            programs = {}
            settings = {}
            for program in commands["startProgram"]:
                program_attr = commands["startProgram"][program]
                program_name = program.split(".")[-1].lower()
                if program_name.endswith("_steam") or program_name.find("_dash_") != -1:
                    continue
                program_params = {}
                for param in program_attr["parameters"]:
                    configs = program_attr["parameters"][param]
                    if (param in ["delayTime", "lang", "waterHard"]):
                        if param not in settings:
                            settings[param] = self.get_param_config(param, configs)
                        program_params[param] = {}
                        continue
                    program_params[param] = self.get_param_config(param, configs)
                description = await self.get_translation(program_attr["description"])
                program_data = {"info": description, "params": program_params}
                if "remainingTimes" in program_attr:
                    program_data["timing"] = program_attr["remainingTimes"]
                programs[program_name] = program_data
            if not programs:
                _LOGGER.warning("No programs found for %s", self._mac)
                return
            await self._hass.async_add_executor_job(self.set_storage_data, CONF_PROGRAMS, programs)
            await self._hass.async_add_executor_job(self.set_storage_data, CONF_SETTINGS, settings)

        current_program = self.current_program_name
        if not current_program or current_program not in programs:
            current_program = list(programs)[0]
        if current_program != self.current_program_name:
            self.set_current_program(current_program)

    async def update_data(self, data):
        attributes = {}

        _LOGGER.debug("update_data %s: %s", self._mac, data)

        for name, values in data.items():
            if not isinstance(values, dict) or "parNewVal" not in values:
                continue
            attributes[name] = values["parNewVal"]

            if name == "prPhase":
                if self._type_name.upper() == "WM":
                    if attributes[name] in ["0","10"]: # Ready
                        attributes[name] = "0"
                    if attributes[name] in ["1","2","14","15","16","25","27"]: # Wash
                        attributes[name] = "1"
                    if attributes[name] in ["3","11"]: # Spin
                        attributes[name] = "3"
                    if attributes[name] in ["4","5","6","17","18"]: # Rinse
                        attributes[name] = "4"
                    if attributes[name] in ["7","8"]: # Drying
                        attributes[name] = "7"
                    if attributes[name] in ["12","13"]: # Weighing
                        attributes[name] = "12"
                if self._type_name.upper() == "TD":
                    if attributes[name] in ["0","11"]: # Ready
                        attributes[name] = "0"
                    if attributes[name] in ["1","2","14","15","19","20"]: # Drying
                        attributes[name] = "1"
                    if attributes[name] in ["3","13","16"]: # Cooldown
                        attributes[name] = "3"
                    if attributes[name] in ["8","12","17"]: # Unknown
                        attributes[name] = "8"

        previous_mode = to_int(self.get_data("machMode"))
        if to_int(attributes.get("machMode")) == 7 and previous_mode == 2:
            await self.send_notify(self._translations.get("component.hon.entity.binary_sensor.notify.state.finished", "finished"))
            self._manually_detergent_notify = False
            self._manually_softener_notify = False

        softener_remaining_time = 20
        if self.get_yaml_config(CONF_SOFTENER_REMAINING_TIME):
            softener_remaining_time = self.get_yaml_config(CONF_SOFTENER_REMAINING_TIME)
        remaining = to_int(attributes.get("remainingTimeMM"))
        if self._manually_softener_notify and previous_mode == 2 and remaining is not None and remaining < softener_remaining_time:
            await self.send_pause_resume()
            await self.send_notify(self._translations.get("component.hon.entity.binary_sensor.notify.state.autosoftener_manually", "autosoftener_manually"))
            self._manually_softener_notify = False

        self.set_data(attributes)


    async def get_status(self):
        result = await self._hon.get_status(self)
        if not result:
            return
        data = dict((result.get("shadow") or {}).get("parameters") or {})
        connection = (result.get("lastConnEvent") or {}).get("category")
        if connection:
            data["lastConnEvent"] = {"parNewVal": connection}
        if data:
            await self.update_data(data)

    async def send_start(self):
        if (not self.current_program_name) or (not self.current_program_settings):
            return

        params = self.current_program_settings

        if not self._low_detergent_notify and "autoDetergentStatus" in params and int(params["autoDetergentStatus"]) == 1 and self.get_data("detWarn") != None:
            if int(self.get_data("detWarn")) == 1:
                await self.send_notify(self._translations.get("component.hon.entity.binary_sensor.notify.state.autodetergent_level", "autodetergent_level"))
                self._low_detergent_notify = True
                return
            else:
                self._low_detergent_notify = False

        if not self._low_softener_notify and "autoSoftenerStatus" in params and int(params["autoSoftenerStatus"]) == 1 and self.get_data("softWarn") != None:
            if int(self.get_data("softWarn")) == 1:
                await self.send_notify(self._translations.get("component.hon.entity.binary_sensor.notify.state.autosoftener_level", "autosoftener_level"))
                self._low_softener_notify = True
                return
            else:
                self._low_softener_notify = False

        if not self._manually_detergent_notify and "autoDetergentStatus" in params and int(params["autoDetergentStatus"]) != 1:
            await self.send_notify(self._translations.get("component.hon.entity.binary_sensor.notify.state.autodetergent_manually", "autodetergent_manually"))
            self._manually_detergent_notify = True
            return

        if not self._manually_softener_notify and "autoSoftenerStatus" in params and int(params["autoSoftenerStatus"]) != 1:
            self._manually_softener_notify = True

        result = await self._hon.send_command(self, "startProgram", params, self.current_program_name)
        # if result:
        #     new_mode = "2"
        #     if int(self.current_program_settings["delayTime"]) > 0:
        #         new_mode = "4"
        #     self.set_data({"machMode": new_mode})


    async def send_stop(self):
        result = await self._hon.send_command(self, "stopProgram", {"onOffStatus": "0"})
        if result:
            # self.set_data({"machMode": "1"})
            self._manually_detergent_notify = False
            self._manually_softener_notify = False


    async def send_pause_resume(self):
        mode = self.get_data("machMode")

        if mode not in ["2","3"]:
            return

        pause = "1"
        command = "pauseProgram"
        new_mode = "3"
        if mode == "3":
            pause = "0"
            command = "resumeProgram"
            new_mode = "2"

        result = await self._hon.send_command(self, command, {"pause": pause})
        # if result:
        #     self.set_data({"machMode": new_mode})
