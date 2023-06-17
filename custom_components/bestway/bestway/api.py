"""Bestway API."""
from dataclasses import dataclass
import json
from logging import getLogger
from time import time

from typing import Any

from aiohttp import ClientResponse, ClientSession
import async_timeout

from .model import (
    BestwayDevice,
    BestwayDeviceStatus,
    BestwayDeviceType,
    BestwayPoolFilterDeviceStatus,
    BestwaySpaDeviceStatus,
    BestwayUserToken,
    TemperatureUnit,
)

_LOGGER = getLogger(__name__)
_HEADERS = {
    "Content-type": "application/json; charset=UTF-8",
    "X-Gizwits-Application-Id": "98754e684ec045528b073876c34c7348",
}
_TIMEOUT = 10


@dataclass
class BestwayDeviceReport:
    """A device report, which combines device metadata with a current status snapshot."""

    device: BestwayDevice
    status: BestwayDeviceStatus | None


@dataclass
class BestwaySpaDeviceReport:
    """A spa device report, which combines device metadata with a current status snapshot."""

    device: BestwayDevice
    status: BestwaySpaDeviceStatus | None


@dataclass
class BestwayPoolFilterDeviceReport:
    """A pump device report, which combines device metadata with a current status snapshot."""

    device: BestwayDevice
    status: BestwayPoolFilterDeviceStatus | None


@dataclass
class BestwayUnknownDeviceReport:
    """An unknown device report, which combines device metadata with raw status JSON."""

    device: BestwayDevice
    status: str | None


class BestwayApiResults:
    """A snapshot of device status reports returned from the API."""

    devices: dict[str, BestwayDevice] = {}
    spa_devices: dict[str, BestwaySpaDeviceReport] = {}
    pool_filter_devices: dict[str, BestwayPoolFilterDeviceReport] = {}
    unknown_devices: dict[str, BestwayUnknownDeviceReport] = {}


class BestwayException(Exception):
    """An exception while using the API."""


class BestwayOfflineException(BestwayException):
    """Device is offline."""

    def __init__(self) -> None:
        """Construct the exception."""
        super().__init__("Device is offline")


class BestwayAuthException(BestwayException):
    """An authentication error."""


class BestwayTokenInvalidException(BestwayAuthException):
    """Auth token is invalid or expired."""


class BestwayUserDoesNotExistException(BestwayAuthException):
    """User does not exist."""


class BestwayIncorrectPasswordException(BestwayAuthException):
    """Password is incorrect."""


async def raise_for_status(response: ClientResponse) -> None:
    """Raise an exception based on the response."""
    if response.ok:
        return

    # Try to parse out the bestway error code
    try:
        api_error = await response.json()
    except Exception:  # pylint: disable=broad-except
        response.raise_for_status()

    error_code = api_error.get("error_code", 0)
    if error_code == 9004:
        raise BestwayTokenInvalidException()
    if error_code == 9005:
        raise BestwayUserDoesNotExistException()
    if error_code == 9042:
        raise BestwayOfflineException()
    if error_code == 9020:
        raise BestwayIncorrectPasswordException()

    # If we don't understand the error code, provide more detail for debugging
    response.raise_for_status()


class BestwayApi:
    """Bestway API."""

    def __init__(self, session: ClientSession, user_token: str, api_root: str) -> None:
        """Initialize the API with a user token."""
        self._session = session
        self._user_token = user_token
        self._api_root = api_root

        # Maps device IDs to device info
        self.devices: dict[str, BestwayDevice] = {}

        # Cache containing state information for each device received from the API
        # This is used to work around an annoyance where changes to settings via
        # a POST request are not immediately reflected in a subsequent GET request.
        #
        # When updating state via HA, we update the cache and return this value
        # until the API can provide us with a response containing a timestamp
        # more recent than the local update.
        self._local_state_cache: dict[str, BestwayDeviceStatus] = {}

    @staticmethod
    async def get_user_token(
        session: ClientSession, username: str, password: str, api_root: str
    ) -> BestwayUserToken:
        """
        Login and obtain a user token.

        The server rate-limits requests for this fairly aggressively.
        """
        body = {"username": username, "password": password, "lang": "en"}

        async with async_timeout.timeout(_TIMEOUT):
            response = await session.post(
                f"{api_root}/app/login", headers=_HEADERS, json=body
            )
            await raise_for_status(response)
            api_data = await response.json()

        return BestwayUserToken(
            api_data["uid"], api_data["token"], api_data["expire_at"]
        )

    async def refresh_bindings(self) -> None:
        """Refresh and store the list of devices available in the account."""
        self.devices = {
            device.device_id: device for device in await self._get_bindings()
        }

    async def _get_bindings(self) -> list[BestwayDevice]:
        """Get the list of devices available in the account."""
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        api_data = await self._do_get(f"{self._api_root}/app/bindings", headers)
        return [
            BestwayDevice(
                raw["protoc"],
                raw["did"],
                raw["product_name"],
                raw["dev_alias"],
                raw["mcu_soft_version"],
                raw["mcu_hard_version"],
                raw["wifi_soft_version"],
                raw["is_online"],
            )
            for raw in api_data["devices"]
        ]

    async def fetch_data(self) -> BestwayApiResults:
        """Fetch the latest data for all devices."""

        results = BestwayApiResults()

        if not self.devices:
            return results

        for did, device_info in self.devices.items():
            latest_data = await self._do_get(
                f"{self._api_root}/app/devdata/{did}/latest", _HEADERS
            )

            # Get the age of the data according to the API
            api_update_timestamp = latest_data["updated_at"]

            # Zero indicates the device is offline
            # This has been observed after a device was offline for a few months
            if api_update_timestamp == 0:
                # In testing, the 'attrs' dictionary has been observed to be empty
                _LOGGER.debug("No data available for device %s", did)

                if device_info.device_type == BestwayDeviceType.AIRJET_SPA:
                    results.spa_devices[did] = BestwaySpaDeviceReport(device_info, None)
                elif device_info.device_type == BestwayDeviceType.POOL_FILTER:
                    results.pool_filter_devices[did] = BestwayPoolFilterDeviceReport(
                        device_info, None
                    )
                elif device_info.device_type == BestwayDeviceType.UNKNOWN:
                    results.unknown_devices[did] = BestwayUnknownDeviceReport(
                        device_info, None
                    )
                continue

            # Work out whether the received API update is more recent than the
            # locally cached state
            local_update_timestamp = 0
            if cached_state := self._local_state_cache.get(did):
                local_update_timestamp = cached_state.timestamp

            # If the API timestamp is more recent, update the cache
            if api_update_timestamp < local_update_timestamp:
                _LOGGER.debug(
                    "Ignoring update for device %s as local data is newer", did
                )
                continue

            _LOGGER.debug("New data received for device %s", did)
            device_attrs = latest_data["attr"]

            try:
                errors = []
                for err_num in range(1, 10):
                    if device_attrs[f"system_err{err_num}"] == 1:
                        errors.append(err_num)

                if device_info.device_type == BestwayDeviceType.AIRJET_SPA:
                    spa_status = BestwaySpaDeviceStatus(
                        latest_data["updated_at"],
                        device_attrs["temp_now"],
                        device_attrs["temp_set"],
                        (
                            TemperatureUnit.CELSIUS
                            if device_attrs["temp_set_unit"]
                            == "摄氏"  # Chinese translates to "Celsius"
                            else TemperatureUnit.FAHRENHEIT
                        ),
                        device_attrs["heat_power"] == 1,
                        device_attrs["heat_temp_reach"] == 1,
                        device_attrs["filter_power"] == 1,
                        device_attrs["wave_power"] == 1,
                        device_attrs["locked"] == 1,
                        errors,
                        device_attrs["earth"] == 1,
                    )

                    self._local_state_cache[did] = spa_status
                    results.spa_devices[did] = BestwaySpaDeviceReport(
                        device_info,
                        spa_status,
                    )

                elif device_info.device_type == BestwayDeviceType.POOL_FILTER:
                    filter_status = BestwayPoolFilterDeviceStatus(
                        latest_data["updated_at"],
                        device_attrs["filter"] == 1,
                        device_attrs["power"] == 1,
                        device_attrs["time"],
                        device_attrs["status"] == "\u8fd0\u884c\u4e2d",
                        errors,
                    )

                    self._local_state_cache[did] = filter_status
                    results.pool_filter_devices[did] = BestwayPoolFilterDeviceReport(
                        device_info,
                        filter_status,
                    )

                elif device_info.device_type == BestwayDeviceType.UNKNOWN:
                    attr_dump = json.dumps(device_attrs)
                    _LOGGER.warning(
                        "Status for unknown device type '%s' returned: %s",
                        device_info.product_name,
                        attr_dump,
                    )
                    results.unknown_devices[did] = BestwayUnknownDeviceReport(
                        device_info,
                        attr_dump,
                    )

            except KeyError as err:
                _LOGGER.error(
                    "Unexpected missing key '%s' while decoding device attributes %s",
                    err,
                    json.dumps(device_attrs),
                )

        return results

    async def spa_set_heat(self, device_id: str, heat: bool) -> None:
        """
        Turn the heater on/off on a spa device.

        Turning the heater on will also turn on the filter pump.
        """
        if (cached_state := self._local_state_cache[device_id]) is None:
            raise BestwayException(f"Device '{device_id}' is not recognised")

        if not isinstance(cached_state, BestwaySpaDeviceStatus):
            raise BestwayException("Method expects a spa device type")

        _LOGGER.debug("Setting heater mode to %s", "ON" if heat else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"heat_power": 1 if heat else 0}},
        )
        cached_state.timestamp = int(time())
        cached_state.heat_power = heat
        if heat:
            cached_state.filter_power = True

    async def spa_set_filter(self, device_id: str, filtering: bool) -> None:
        """Turn the filter pump on/off on a spa device."""
        if (cached_state := self._local_state_cache[device_id]) is None:
            raise BestwayException(f"Device '{device_id}' is not recognised")

        if not isinstance(cached_state, BestwaySpaDeviceStatus):
            raise BestwayException("Method expects a spa device type")

        _LOGGER.debug("Setting filter mode to %s", "ON" if filtering else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"filter_power": 1 if filtering else 0}},
        )
        cached_state.timestamp = int(time())
        cached_state.filter_power = filtering
        if not filtering:
            cached_state.wave_power = False
            cached_state.heat_power = False

    async def spa_set_locked(self, device_id: str, locked: bool) -> None:
        """Lock or unlock the physical control panel on a spa device."""
        if (cached_state := self._local_state_cache[device_id]) is None:
            raise BestwayException(f"Device '{device_id}' is not recognised")

        if not isinstance(cached_state, BestwaySpaDeviceStatus):
            raise BestwayException("Method expects a spa device type")

        _LOGGER.debug("Setting lock state to %s", "ON" if locked else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"locked": 1 if locked else 0}},
        )
        cached_state.timestamp = int(time())
        cached_state.locked = locked

    async def spa_set_bubbles(self, device_id: str, bubbles: bool) -> None:
        """Turn the bubbles on/off on a spa device."""
        if (cached_state := self._local_state_cache[device_id]) is None:
            raise BestwayException(f"Device '{device_id}' is not recognised")

        if not isinstance(cached_state, BestwaySpaDeviceStatus):
            raise BestwayException("Method expects a spa device type")

        _LOGGER.debug("Setting bubbles mode to %s", "ON" if bubbles else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"wave_power": 1 if bubbles else 0}},
        )
        cached_state.timestamp = int(time())
        cached_state.filter_power = bubbles
        if bubbles:
            cached_state.filter_power = True

    async def spa_set_target_temp(self, device_id: str, target_temp: int) -> None:
        """Set the target temperature on a spa device."""
        if (cached_state := self._local_state_cache[device_id]) is None:
            raise BestwayException(f"Device '{device_id}' is not recognised")

        if not isinstance(cached_state, BestwaySpaDeviceStatus):
            raise BestwayException("Method expects a spa device type")

        _LOGGER.debug("Setting target temperature to %d", target_temp)
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"temp_set": target_temp}},
        )
        cached_state.timestamp = int(time())
        cached_state.temp_set = target_temp

    async def pool_filter_set_power(self, device_id: str, power: bool) -> None:
        """Control power to a pump device."""
        if (cached_state := self._local_state_cache[device_id]) is None:
            raise BestwayException(f"Device '{device_id}' is not recognised")

        if not isinstance(cached_state, BestwayPoolFilterDeviceStatus):
            raise BestwayException("Method expects a spa device type")

        _LOGGER.debug("Setting power to %s", "ON" if power else "OFF")
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"power": 1 if power else 0}},
        )
        cached_state.timestamp = int(time())
        cached_state.power = power

    async def pool_filter_set_time(self, device_id: str, hours: int) -> None:
        """Set filter timeout for for pool devices."""
        if (cached_state := self._local_state_cache[device_id]) is None:
            raise BestwayException(f"Device '{device_id}' is not recognised")

        if not isinstance(cached_state, BestwayPoolFilterDeviceStatus):
            raise BestwayException("Method expects a spa device type")

        _LOGGER.debug("Setting filter timeout to %d hours", hours)
        headers = dict(_HEADERS)
        headers["X-Gizwits-User-token"] = self._user_token
        await self._do_post(
            f"{self._api_root}/app/control/{device_id}",
            headers,
            {"attrs": {"time": hours}},
        )
        cached_state.timestamp = int(time())
        cached_state.time = hours

    async def _do_get(self, url: str, headers: dict[str, str]) -> dict[str, Any]:
        """Make an API call to the specified URL, returning the response as a JSON object."""
        async with async_timeout.timeout(_TIMEOUT):
            response = await self._session.get(url, headers=headers)
            response.raise_for_status()

            # All API responses are encoded using JSON, however the headers often incorrectly
            # state 'text/html' as the content type.
            # We have to disable the check to avoid an exception.
            response_json: dict[str, Any] = await response.json(content_type=None)
            return response_json

    async def _do_post(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        """Make an API call to the specified URL, returning the response as a JSON object."""
        async with async_timeout.timeout(_TIMEOUT):
            response = await self._session.post(url, headers=headers, json=body)
            await raise_for_status(response)

            # All API responses are encoded using JSON, however the headers often incorrectly
            # state 'text/html' as the content type.
            # We have to disable the check to avoid an exception.
            response_json: dict[str, Any] = await response.json(content_type=None)
            return response_json