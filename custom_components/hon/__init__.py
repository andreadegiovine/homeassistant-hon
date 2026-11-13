import logging
import voluptuous as vol

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import translation

from .const import DOMAIN, PLATFORMS, CONF_MAC, CONF_DISABLED_PROGRAMS, CONF_SOFTENER_REMAINING_TIME
from .hon import HonConnection
from .device import HonDevice

_LOGGER = logging.getLogger(__name__)

HON_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_MAC): cv.string,
        CONF_DISABLED_PROGRAMS: vol.All(cv.ensure_list, [cv.string]),
        vol.Optional(CONF_SOFTENER_REMAINING_TIME): int
    }
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema(
            vol.All(cv.ensure_list, [HON_SCHEMA])
        )
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup_entry(hass, entry):
    hon = HonConnection(hass, entry)
    if not await hon.async_authorize():
        await hon.async_close()
        raise ConfigEntryNotReady("Unable to connect to hOn")

    _LOGGER.debug(f"Appliances: {hon.appliances}")

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.unique_id] = hon

    translations = await translation.async_get_translations(hass, hass.config.language, "entity")

    for appliance in hon.appliances:
        coordinator = hon.async_get_coordinator(appliance)
        device = HonDevice(entry, hon, coordinator, appliance, translations)
        coordinator.device = device
        await device.async_load_storage()
        # Programs must be known before the platforms create select/switch entities
        try:
            await device.get_programs()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unable to load programs for %s", device._mac)
        try:
            await device.get_status()
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unable to load status for %s", device._mac)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await hon.start_mqtt()

    return True


async def async_unload_entry(hass, entry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hon = hass.data.get(DOMAIN, {}).pop(entry.unique_id, None)
        if hon is not None:
            await hon.async_shutdown()
    return unload_ok


async def async_setup(hass, config):

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["configuration_yaml"] = config.get(DOMAIN)

    return True


async def async_migrate_entry(hass, config):
    _LOGGER.debug(f"Current version: {config.version}.{config.minor_version}")

    if config.version == 1 and config.minor_version < 2:
        _LOGGER.debug("Update to 1.2")
        for key in config.data:
            if isinstance(config.data[key], dict):
                if "settings" in config.data[key]:
                    config.data[key]["settings"] = {}
                if "programs" in config.data[key]:
                    config.data[key]["programs"] = {}
        hass.config_entries.async_update_entry(config, data=config.data, minor_version=2)

    if config.version == 1 and config.minor_version < 3:
        _LOGGER.debug("Update to 1.3")
        for key in config.data:
            if isinstance(config.data[key], dict):
                if "settings" in config.data[key]:
                    del config.data[key]["settings"]
                if "programs" in config.data[key]:
                    del config.data[key]["programs"]
                if "options" in config.data[key]:
                    config.data[key]["options"] = {}
        hass.config_entries.async_update_entry(config, data=config.data, minor_version=3)

    return True
