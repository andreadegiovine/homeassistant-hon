import logging
import aiohttp
import secrets
import base64
import hashlib
import json
import time
from datetime import datetime, timedelta, timezone
import asyncio

from awsiot import mqtt5_client_builder
from awscrt import mqtt5

from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.event import async_track_time_interval

from .base import HonBaseCoordinator
from .const import (
    CONF_ID_TOKEN,
    CONF_FRAMEWORK,
    CONF_COGNITO_TOKEN,
    CONF_REFRESH_TOKEN,
    API_URL,
    DEVICE_MODEL,
    APP_VERSION,
    OS,
    OS_VERSION,
    AWS_ENDPOINT,
    AWS_AUTHORIZER
)

_LOGGER = logging.getLogger(__name__)

SESSION_TIMEOUT = 21600  # 6 hours: tokens are renewed before every API call older than this
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=30)

MQTT_WATCHDOG_INTERVAL = timedelta(seconds=30)
MQTT_RECONNECT_GRACE = 90  # seconds without connection before the client is rebuilt
MQTT_SUBSCRIBE_TIMEOUT = 10
STATUS_RESYNC_INTERVAL = 600  # periodic HTTP resync, in case some MQTT message was lost


class HonConnection:
    def __init__(self, hass, entry, email=None, password=None) -> None:
        self._hass = hass
        self._entry = entry
        self._coordinator_dict = {}
        self._mobile_id = secrets.token_hex(8)

        self._id_token = ""
        self._refresh_token = ""
        self._cognitoToken = ""

        # Only used during registration (Login/password check)
        if (email is not None) and (password is not None):
            self._email = email
            self._password = password
            self._framework = "None"
        else:
            self._email = entry.data[CONF_EMAIL]
            self._password = entry.data[CONF_PASSWORD]
            self._framework = entry.data.get(CONF_FRAMEWORK, "")
            self._id_token = entry.data.get(CONF_ID_TOKEN, "")
            self._refresh_token = entry.data.get(CONF_REFRESH_TOKEN, "")
            self._cognitoToken = entry.data.get(CONF_COGNITO_TOKEN, "")

        self._start_time = 0
        self._auth_lock = asyncio.Lock()

        self._header = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
        }
        self._session = aiohttp.ClientSession(headers=self._header, timeout=REQUEST_TIMEOUT)
        self._appliances = []

        # MQTT state
        self._mqtt = None
        self._mqtt_generation = 0
        self._mqtt_connected = False
        self._mqtt_state_since = time.time()
        self._mqtt_needs_rebuild = False
        self._mqtt_lock = asyncio.Lock()
        self._topic_map = {}
        self._unsub_watchdog = None
        self._last_resync = 0

    @property
    def _headers(self):
        return {
            "Content-Type": "application/json",
            "cognito-token": self._cognitoToken,
            "id-token": self._id_token,
        }

    @property
    def appliances(self):
        return self._appliances

    async def async_close(self):
        if not self._session.closed:
            await self._session.close()

    async def async_shutdown(self):
        """Stop MQTT, timers and HTTP session (entry unload / HA stop)."""
        if self._unsub_watchdog:
            self._unsub_watchdog()
            self._unsub_watchdog = None
        async with self._mqtt_lock:
            await self._async_mqtt_stop()
        await self.async_close()

    def async_get_coordinator(self, appliance):
        mac = appliance.get("macAddress", "")
        if mac in self._coordinator_dict:
            return self._coordinator_dict[mac]
        coordinator = HonBaseCoordinator(self._hass, self, appliance)
        self._coordinator_dict[mac] = coordinator
        return coordinator

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_pkce_pair():
        verifier = (
            base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
        )
        digest = hashlib.sha256(verifier.encode()).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
        return verifier, challenge

    async def _get_session_id(self, code_challenge):
        params = {
            "username": self._email,
            "password": self._password,
            "code_challenge": code_challenge,
        }
        async with self._session.get(
                f"{API_URL}/ciam/authorize", params=params
        ) as response:
            if response.status != 200:
                # Never log params: they contain the password
                _LOGGER.error("Unable to get session_id: HTTP %s", response.status)
                return ""
            data = await response.json(content_type=None)
            session_id = (data or {}).get("session_id", "")
            if not session_id:
                _LOGGER.error("session_id missing from /ciam/authorize response")
            return session_id

    async def _get_tokens(self, session_id, code_verifier):
        async with self._session.post(
                f"{API_URL}/ciam/token",
                json={"session_id": session_id, "code_verifier": code_verifier},
        ) as response:
            if response.status != 200:
                _LOGGER.error("Unable to get tokens: HTTP %s", response.status)
                return False
            tokens = ((await response.json(content_type=None)) or {}).get("tokens", {})
            id_token = tokens.get("id_token", "")
            cognito_token = tokens.get("cognito_token", "")
            if not (id_token and cognito_token):
                _LOGGER.error("Tokens missing from /ciam/token response")
                return False
            self._id_token = id_token
            self._cognitoToken = cognito_token
            self._refresh_token = tokens.get("refresh_token", "")
            return True

    async def _async_login(self, min_age=0, rejected_token=None):
        """Get new tokens.

        min_age: skip if the current tokens are younger than min_age seconds.
        rejected_token: skip if the token rejected by the server was already replaced
        (another call refreshed it concurrently).
        """
        async with self._auth_lock:
            if min_age and self._id_token and (time.time() - self._start_time) < min_age:
                return True
            if rejected_token is not None and self._id_token and self._id_token != rejected_token:
                return True

            self._session.cookie_jar.clear()
            code_verifier, code_challenge = self._generate_pkce_pair()
            try:
                session_id = await self._get_session_id(code_challenge)
                if not session_id:
                    _LOGGER.error("Can't get session id")
                    return False
                if not await self._get_tokens(session_id, code_verifier):
                    _LOGGER.error("Can't get api tokens")
                    return False
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as err:
                _LOGGER.error("hOn login failed: %s", err)
                return False

            self._start_time = time.time()
            _LOGGER.debug("hOn login successful")
            return True

    async def _async_ensure_session(self):
        if not self._id_token or (time.time() - self._start_time) > SESSION_TIMEOUT:
            await self._async_login(min_age=SESSION_TIMEOUT)

    async def async_authorize(self):
        """Login and load the appliance list (setup / config flow)."""
        if not await self._async_login():
            return False

        data = await self._async_request(
            "POST",
            "/unified-api/v1/view/appliance-list",
            json={"deviceId": self._mobile_id},
        )
        if data is None:
            _LOGGER.error("hOn Invalid Data after GET appliance-list")
            return False

        appliances = (
            data.get("modules", {})
            .get("applianceList", {})
            .get("payload", {})
            .get("appliances", [])
        )
        _LOGGER.debug(f"All appliances: {appliances}")

        # Keep only WM / TD appliances with mac and type id
        self._appliances = [
            a for a in appliances
            if "macAddress" in a
            and "applianceTypeId" in a
            and a.get("applianceTypeName") in ["WM", "TD"]
        ]
        return True

    # ------------------------------------------------------------------
    # HTTP API
    # ------------------------------------------------------------------

    async def _async_request(self, method, path, *, retry=True, **kwargs):
        """Authenticated API call. Returns the decoded JSON dict, or None on any error.

        Tokens are renewed when older than SESSION_TIMEOUT and, if the server still
        rejects them (401/403), a new login is done and the call is retried once.
        """
        await self._async_ensure_session()
        url = f"{API_URL}{path}"
        used_token = self._id_token
        try:
            async with self._session.request(method, url, headers=self._headers, **kwargs) as resp:
                status = resp.status
                text = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.error("hOn request %s %s failed: %s", method, path, err)
            return None

        if status in (401, 403) and retry:
            _LOGGER.debug("hOn %s %s: HTTP %s, logging in again", method, path, status)
            if await self._async_login(rejected_token=used_token):
                return await self._async_request(method, path, retry=False, **kwargs)
            return None

        if status != 200:
            _LOGGER.error("hOn %s %s: HTTP %s [%s]", method, path, status, text[:300])
            return None

        try:
            data = json.loads(text)
        except ValueError:
            _LOGGER.error("hOn %s %s: invalid JSON response [%s]", method, path, text[:300])
            return None

        if not isinstance(data, dict):
            _LOGGER.error("hOn %s %s: unexpected response [%s]", method, path, text[:300])
            return None
        return data

    async def get_programs(self, appliance):
        params = {
            "applianceType": appliance["applianceTypeId"],
            "code": appliance["code"],
            "applianceModelId": appliance["applianceModelId"],
            "firmwareId": appliance["eepromId"],
            "macAddress": appliance["macAddress"],
            "fwVersion": appliance["fwVersion"],
            "os": OS,
            "appVersion": APP_VERSION,
            "series": appliance["series"],
        }
        data = await self._async_request("GET", "/commands/v1/retrieve", params=params)
        result = (data or {}).get("payload", {})
        if not result or result.pop("resultCode", None) != "0":
            return {}
        _LOGGER.debug(f"Commands: {result}")
        return result

    async def get_status(self, device):
        params = {
            "macAddress": device._mac_address,
            "applianceType": device._type_name,
            "category": "CYCLE"
        }
        data = await self._async_request("GET", "/commands/v1/context", params=params)
        _LOGGER.debug(f"Context for mac[{device._mac_address}] type [{device._type_name}] {data}")
        return (data or {}).get("payload", {})

    async def send_command(self, device, command, parameters, program_name=False):
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
        args = {
            "macAddress": device._mac_address,
            "attributes": {
                "channel": "mobileApp",
                "origin": "standardProgram"
            },
            "device": {
                "mobileOs": OS,
                "osVersion": OS_VERSION,
                "appVersion": APP_VERSION,
                "deviceModel": DEVICE_MODEL,
                "mobileId": self._mobile_id
            },
            "ancillaryParameters": {},
            "applianceOptions": {},
            "transactionId": f"{device._mac_address}_{now}Z",
            "commandName": command,
            "parameters": parameters
        }

        if program_name:
            args["attributes"]["energyLabel"] = "0"
            program_name = program_name.upper()
            type = device._type_name.upper()
            if type == "WM":
                type = f"{type}_WD"
            args["programName"] = f"PROGRAMS.{type}.{program_name}"

        _LOGGER.debug("Send command %s", args)

        data = await self._async_request("POST", "/commands/v1/send", json=args)
        if data is None:
            _LOGGER.error("hOn command [%s] not sent: invalid response", command)
            return False

        _LOGGER.debug(f"Command result (send_command): {data}")
        if (data.get("payload") or {}).get("resultCode") == "0":
            return True
        _LOGGER.error("hOn command has been rejected. Error message [%s] sent command [%s]", data, command)
        return False

    async def get_aws_token(self) -> str:
        data = await self._async_request("GET", "/auth/v1/introspection")
        return ((data or {}).get("payload") or {}).get("tokenSigned", "")

    async def async_refresh_all_status(self):
        """Read the full status of every appliance via HTTP."""
        self._last_resync = time.time()
        for coordinator in list(self._coordinator_dict.values()):
            if coordinator.device is None:
                continue
            try:
                await coordinator.device.get_status()
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unable to refresh status of %s", coordinator.device._mac)

    # ------------------------------------------------------------------
    # MQTT
    # ------------------------------------------------------------------

    async def start_mqtt(self):
        self._topic_map = {}
        for appliance in self.appliances:
            for topic in appliance.get("topics", {}).get("subscribe", []):
                self._topic_map[topic] = appliance["macAddress"]

        if not await self._async_mqtt_connect():
            self._mqtt_needs_rebuild = True

        self._last_resync = time.time()
        self._unsub_watchdog = async_track_time_interval(
            self._hass, self._async_watchdog, MQTT_WATCHDOG_INTERVAL
        )

    async def _async_mqtt_stop(self):
        """Stop the current client. Must be called with _mqtt_lock held."""
        # New generation: late callbacks from the old client are ignored
        self._mqtt_generation += 1
        self._mqtt_connected = False
        client, self._mqtt = self._mqtt, None
        if client is None:
            return
        try:
            await self._hass.async_add_executor_job(client.stop)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Error stopping mqtt client: %s", err)

    async def _async_mqtt_connect(self):
        """(Re)create the MQTT client with fresh credentials."""
        async with self._mqtt_lock:
            await self._async_mqtt_stop()
            self._mqtt_state_since = time.time()

            signature = await self.get_aws_token()
            if not signature:
                _LOGGER.warning("Unable to get aws token, retrying with a new login")
                if await self._async_login():
                    signature = await self.get_aws_token()
            if not signature:
                _LOGGER.error("Mqtt connection error: no aws token")
                return False

            generation = self._mqtt_generation
            try:
                client = mqtt5_client_builder.websockets_with_custom_authorizer(
                    endpoint=AWS_ENDPOINT,
                    auth_authorizer_name=AWS_AUTHORIZER,
                    auth_authorizer_signature=signature,
                    auth_token_key_name="token",
                    auth_token_value=self._id_token,
                    client_id=f"pyhOn_{self._mobile_id}",
                    on_lifecycle_connection_success=lambda data, g=generation: self._on_mqtt_connect(g, data),
                    on_lifecycle_connection_failure=lambda data, g=generation: self._on_mqtt_failure(g, data),
                    on_lifecycle_disconnection=lambda data, g=generation: self._on_mqtt_disconnect(g, data),
                    on_publish_received=lambda data, g=generation: self._on_mqtt_message(g, data),
                )
                self._mqtt = client
                await self._hass.async_add_executor_job(client.start)
            except Exception as err:  # noqa: BLE001
                _LOGGER.error("Mqtt connection error %s", err)
                self._mqtt = None
                return False
            return True

    async def _async_mqtt_subscribe(self, generation):
        """Subscribe to every topic. Called after *each* connection success,
        because the session is clean and the broker forgets subscriptions."""
        if generation != self._mqtt_generation or self._mqtt is None:
            return
        client = self._mqtt
        try:
            for topic in self._topic_map:
                future = client.subscribe(
                    mqtt5.SubscribePacket([mqtt5.Subscription(topic)])
                )
                suback = await asyncio.wait_for(
                    asyncio.wrap_future(future), MQTT_SUBSCRIBE_TIMEOUT
                )
                codes = getattr(suback, "reason_codes", []) or []
                if any(int(code) >= 0x80 for code in codes):
                    raise RuntimeError(f"subscription to {topic} refused: {codes}")
                _LOGGER.debug("Subscribed to %s", topic)
        except Exception as err:  # noqa: BLE001
            if generation == self._mqtt_generation:
                _LOGGER.warning("Mqtt subscribe failed (%s), client will be rebuilt", err)
                self._mqtt_needs_rebuild = True
            return

        # Recover anything that changed while we were not subscribed
        await self.async_refresh_all_status()

    def _run_on_loop(self, coro):
        """Schedule a coroutine on the HA loop from an MQTT (CRT) thread, without blocking it."""
        future = asyncio.run_coroutine_threadsafe(coro, self._hass.loop)

        def _done(fut):
            if not fut.cancelled() and fut.exception() is not None:
                _LOGGER.error("Error handling mqtt event", exc_info=fut.exception())

        future.add_done_callback(_done)

    def _on_mqtt_connect(self, generation, data):
        if generation != self._mqtt_generation:
            return
        _LOGGER.debug("_on_mqtt_connect - %s", str(data))
        self._mqtt_connected = True
        self._mqtt_state_since = time.time()
        self._run_on_loop(self._async_mqtt_subscribe(generation))

    def _on_mqtt_failure(self, generation, data):
        if generation != self._mqtt_generation:
            return
        _LOGGER.debug("_on_mqtt_failure - %s", str(getattr(data, "exception", data)))
        if self._mqtt_connected:
            self._mqtt_state_since = time.time()
        self._mqtt_connected = False

    def _on_mqtt_disconnect(self, generation, data):
        if generation != self._mqtt_generation:
            return
        _LOGGER.debug("_on_mqtt_disconnect - %s", str(getattr(data, "exception", data)))
        if self._mqtt_connected:
            self._mqtt_state_since = time.time()
        self._mqtt_connected = False

    def _on_mqtt_message(self, generation, data):
        if generation != self._mqtt_generation:
            return
        try:
            packet = data.publish_packet if data else None
            if not (packet and packet.payload):
                return
            topic = packet.topic or ""
            raw = packet.payload.decode() if isinstance(packet.payload, (bytes, bytearray)) else str(packet.payload)
            _LOGGER.debug("_on_mqtt_message - topic %s - payload %s", topic, raw)

            mac = self._topic_map.get(topic)
            coordinator = self._coordinator_dict.get(mac) if mac else None
            if coordinator is None or coordinator.device is None:
                _LOGGER.debug("appliance not found for topic %s", topic)
                return

            update = {}
            if "appliancestatus" in topic:
                try:
                    payload = json.loads(raw)
                except ValueError:
                    _LOGGER.warning("Invalid JSON on topic %s: %s", topic, raw[:300])
                    return
                parameters = payload.get("parameters", []) if isinstance(payload, dict) else []
                for parameter in parameters:
                    if isinstance(parameter, dict) and "parName" in parameter and "parNewVal" in parameter:
                        update[parameter["parName"]] = parameter
            elif "disconnected" in topic:
                update = {"lastConnEvent": {"parNewVal": "DISCONNECTED"}}
            elif "connected" in topic:
                update = {"lastConnEvent": {"parNewVal": "CONNECTED"}}

            if update:
                self._run_on_loop(coordinator.device.update_data(update))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Error parsing mqtt message")

    async def _async_watchdog(self, now=None):
        if self._mqtt_lock.locked():
            return
        now_ts = time.time()
        disconnected_for = now_ts - self._mqtt_state_since
        if self._mqtt_needs_rebuild or (not self._mqtt_connected and disconnected_for > MQTT_RECONNECT_GRACE):
            _LOGGER.info(
                "Mqtt not connected for %ss (rebuild requested: %s), reconnecting",
                int(disconnected_for), self._mqtt_needs_rebuild,
            )
            self._mqtt_needs_rebuild = False
            if not await self._async_mqtt_connect():
                # Retry after MQTT_RECONNECT_GRACE seconds
                self._mqtt_state_since = time.time()
            return

        if now_ts - self._last_resync > STATUS_RESYNC_INTERVAL:
            await self.async_refresh_all_status()
