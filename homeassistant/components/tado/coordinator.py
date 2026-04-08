"""Coordinator for the Tado integration."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from PyTado.interface import Tado
from requests import RequestException

from homeassistant.components.climate import PRESET_AWAY, PRESET_HOME
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_FALLBACK,
    CONF_REFRESH_TOKEN,
    CONST_OVERLAY_TADO_DEFAULT,
    DOMAIN,
    INSIDE_TEMPERATURE_MEASUREMENT,
    PRESET_AUTO,
    TEMP_OFFSET,
    TYPE_HEATING,
)

_LOGGER = logging.getLogger(__name__)

MIN_TIME_BETWEEN_UPDATES = timedelta(minutes=4)
SCAN_INTERVAL = timedelta(minutes=5)

type TadoConfigEntry = ConfigEntry[TadoDataUpdateCoordinator]


class TadoDataUpdateCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """Class to manage API calls from and to Tado via PyTado."""

    tado: Tado
    home_id: int
    home_name: str
    config_entry: TadoConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: TadoConfigEntry,
        tado: Tado,
        debug: bool = False,
    ) -> None:
        """Initialize the Tado data update coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=SCAN_INTERVAL,
        )
        self._tado = tado
        self._refresh_token = config_entry.data[CONF_REFRESH_TOKEN]
        self._fallback = config_entry.options.get(
            CONF_FALLBACK, CONST_OVERLAY_TADO_DEFAULT
        )
        self._debug = debug

        self.home_id: int
        self.home_name: str
        self.zones: list[dict[Any, Any]] = []
        self.devices: list[dict[Any, Any]] = []
        self.data: dict[str, dict] = {
            "device": {},
            "weather": {},
            "geofence": {},
            "zone": {},
        }

    @property
    def fallback(self) -> str:
        """Return fallback flag to Smart Schedule."""
        return self._fallback

    @property
    def is_tadox(self) -> bool:
        """Return True if connected to a TadoX system (hops.tado.com API)."""
        return bool(getattr(self._tado, "_http", None) and self._tado._http.is_x_line)

    async def _async_update_data(self) -> dict[str, dict]:
        """Fetch the (initial) latest data from Tado."""
        try:
            _LOGGER.debug("Preloading home data")
            tado_home_call = await self.hass.async_add_executor_job(self._tado.get_me)
            _LOGGER.debug("Preloading zones and devices")
            raw_zones = await self.hass.async_add_executor_job(self._tado.get_zones)
            self.zones = self._normalize_zones(raw_zones)
            raw_devices = await self.hass.async_add_executor_job(
                self._tado.get_devices
            )
            self.devices = self._normalize_devices(raw_devices)
        except RequestException as err:
            _LOGGER.debug("Checking rate limit")
            ratelimit = self.get_rate_limit()
            if ratelimit.get("remaining") == "0":
                raise UpdateFailed(f"Tado API rate limit reached: {err}") from err
            raise UpdateFailed(f"Error during Tado setup: {err}") from err

        tado_home = tado_home_call["homes"][0]
        self.home_id = tado_home["id"]
        self.home_name = tado_home["name"]

        devices = await self._async_update_devices()
        zones = await self._async_update_zones()
        home = await self._async_update_home()

        self.data["device"] = devices
        self.data["zone"] = zones
        self.data["weather"] = home["weather"]
        self.data["geofence"] = home["geofence"]

        refresh_token = await self.hass.async_add_executor_job(
            self._tado.get_refresh_token
        )

        if refresh_token != self._refresh_token:
            _LOGGER.debug("New refresh token obtained from Tado: %s", refresh_token)
            self._refresh_token = refresh_token
            self.hass.config_entries.async_update_entry(
                self.config_entry,
                data={**self.config_entry.data, CONF_REFRESH_TOKEN: refresh_token},
            )

        return self.data

    @staticmethod
    def _backfill_device_keys(device: dict[str, Any]) -> None:
        """Backfill old API keys on a single device dict for downstream compatibility."""
        serial = device.get("shortSerialNo") or device.get("serialNumber")
        if serial:
            device.setdefault("shortSerialNo", serial)
            device.setdefault("serialNo", serial)
        if "deviceType" not in device and "type" in device:
            device["deviceType"] = device["type"]
        if "currentFwVersion" not in device and "firmwareVersion" in device:
            device["currentFwVersion"] = device["firmwareVersion"]
        # TadoX returns TEMP_OFFSET as a plain float; wrap for downstream code
        if TEMP_OFFSET in device and isinstance(device[TEMP_OFFSET], (int, float)):
            val = device[TEMP_OFFSET]
            device[TEMP_OFFSET] = {"celsius": val, "fahrenheit": val * 1.8}

    @staticmethod
    def _normalize_devices(raw_devices: list) -> list[dict[str, Any]]:
        """Flatten and normalize the raw device list from the Tado API."""
        flat: list[dict[str, Any]] = []
        for entry in raw_devices:
            if isinstance(entry, list):
                flat.extend(d for d in entry if isinstance(d, dict))
            elif isinstance(entry, dict):
                flat.append(entry)
        for device in flat:
            TadoDataUpdateCoordinator._backfill_device_keys(device)
        return flat

    @staticmethod
    def _normalize_zones(raw_zones: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Normalize zone data from both old and new Tado API formats.

        The new TadoX API returns rooms with different field names.
        This backfills old API keys so downstream code works unchanged.
        """
        normalized = []
        for zone in raw_zones:
            # TadoX uses roomId/roomName instead of id/name
            if "id" not in zone and "roomId" in zone:
                zone["id"] = zone["roomId"]
            if "name" not in zone and "roomName" in zone:
                zone["name"] = zone["roomName"]
            # Old API has 'type', new API (TadoX) does not
            if "type" not in zone:
                zone["type"] = TYPE_HEATING
            # Ensure 'devices' key exists
            if "devices" not in zone:
                zone["devices"] = []
            # Backfill old API keys on each device in the zone
            for device in zone["devices"]:
                if isinstance(device, dict):
                    TadoDataUpdateCoordinator._backfill_device_keys(device)
            normalized.append(zone)
        return normalized

    async def _async_update_devices(self) -> dict[str, dict]:
        """Update the device data from Tado."""

        try:
            devices = await self.hass.async_add_executor_job(self._tado.get_devices)
        except RequestException as err:
            _LOGGER.error("Error updating Tado devices: %s", err)
            raise UpdateFailed(f"Error updating Tado devices: {err}") from err

        if not devices:
            _LOGGER.error("No linked devices found for home ID %s", self.home_id)
            raise UpdateFailed(f"No linked devices found for home ID {self.home_id}")

        return await self.hass.async_add_executor_job(self._update_device_info, devices)

    def _update_device_info(self, devices: list[dict[str, Any]]) -> dict[str, dict]:
        """Update the device data from Tado."""
        mapped_devices: dict[str, dict] = {}
        flat_devices = self._normalize_devices(devices)
        for device in flat_devices:
            device_short_serial_no = device.get("shortSerialNo")
            if not device_short_serial_no:
                continue
            _LOGGER.debug("Updating device %s", device_short_serial_no)
            try:
                characteristics = device.get("characteristics", {})
                capabilities = characteristics.get("capabilities", [])
                if INSIDE_TEMPERATURE_MEASUREMENT in capabilities:
                    _LOGGER.debug(
                        "Updating temperature offset for device %s",
                        device_short_serial_no,
                    )
                    offset = self._tado.get_device_info(
                        device_short_serial_no, TEMP_OFFSET
                    )
                    # TadoX returns a plain float; wrap it for downstream code
                    if isinstance(offset, (int, float)):
                        offset = {"celsius": offset, "fahrenheit": offset * 1.8}
                    device[TEMP_OFFSET] = offset
            except RequestException as err:
                _LOGGER.error(
                    "Error updating device %s: %s", device_short_serial_no, err
                )

            _LOGGER.debug(
                "Device %s updated, with data: %s", device_short_serial_no, device
            )
            mapped_devices[device_short_serial_no] = device

        return mapped_devices

    async def _async_update_zones(self) -> dict[int, dict]:
        """Update the zone data from Tado."""

        try:
            zone_states_call = await self.hass.async_add_executor_job(
                self._tado.get_zone_states
            )
            if isinstance(zone_states_call, dict):
                zone_states = zone_states_call["zoneStates"]
            elif isinstance(zone_states_call, list):
                # TadoX API returns a list of zone state dicts with 'id' key
                zone_states = {
                    str(state["id"]): state
                    for state in zone_states_call
                    if isinstance(state, dict) and "id" in state
                }
            else:
                raise UpdateFailed(f"Unexpected zone_states type: {type(zone_states_call)}")
        except RequestException as err:
            _LOGGER.error("Error updating Tado zones: %s", err)
            raise UpdateFailed(f"Error updating Tado zones: {err}") from err

        mapped_zones: dict[int, dict] = {}
        for zone in zone_states:
            mapped_zones[int(zone)] = await self._update_zone(int(zone))

        return mapped_zones

    async def _update_zone(self, zone_id: int) -> dict[str, str]:
        """Update the internal data of a zone."""

        _LOGGER.debug("Updating zone %s", zone_id)
        try:
            data = await self.hass.async_add_executor_job(
                self._tado.get_zone_state, zone_id
            )
        except RequestException as err:
            _LOGGER.error("Error updating Tado zone %s: %s", zone_id, err)
            raise UpdateFailed(f"Error updating Tado zone {zone_id}: {err}") from err

        _LOGGER.debug("Zone %s updated, with data: %s", zone_id, data)
        return data

    async def _async_update_home(self) -> dict[str, dict]:
        """Update the home data from Tado."""

        try:
            weather = await self.hass.async_add_executor_job(self._tado.get_weather)
            geofence = await self.hass.async_add_executor_job(self._tado.get_home_state)
        except RequestException as err:
            _LOGGER.error("Error updating Tado home: %s", err)
            raise UpdateFailed(f"Error updating Tado home: {err}") from err

        _LOGGER.debug(
            "Home data updated, with weather and geofence data: %s, %s",
            weather,
            geofence,
        )

        return {"weather": weather, "geofence": geofence}

    async def get_capabilities(self, zone_id: int | str) -> dict:
        """Fetch the capabilities from Tado."""

        if self.is_tadox:
            # TadoX API does not support get_capabilities; return defaults
            return {
                "type": TYPE_HEATING,
                "temperatures": {
                    "celsius": {"min": 5.0, "max": 25.0, "step": 0.1},
                },
            }
        try:
            return await self.hass.async_add_executor_job(
                self._tado.get_capabilities, zone_id
            )
        except RequestException as err:
            raise UpdateFailed(f"Error updating Tado data: {err}") from err

    async def get_auto_geofencing_supported(self) -> bool:
        """Fetch the auto geofencing supported from Tado."""

        try:
            return await self.hass.async_add_executor_job(
                self._tado.get_auto_geofencing_supported
            )
        except RequestException as err:
            raise UpdateFailed(f"Error updating Tado data: {err}") from err

    async def reset_zone_overlay(self, zone_id):
        """Reset the zone back to the default operation."""

        try:
            await self.hass.async_add_executor_job(
                self._tado.reset_zone_overlay, zone_id
            )
            await self._update_zone(zone_id)
        except RequestException as err:
            raise UpdateFailed(f"Error resetting Tado data: {err}") from err

    async def set_presence(
        self,
        presence=PRESET_HOME,
    ):
        """Set the presence to home, away or auto."""

        if presence == PRESET_AWAY:
            await self.hass.async_add_executor_job(self._tado.set_away)
        elif presence == PRESET_HOME:
            await self.hass.async_add_executor_job(self._tado.set_home)
        elif presence == PRESET_AUTO:
            await self.hass.async_add_executor_job(self._tado.set_auto)

    async def set_zone_overlay(
        self,
        zone_id=None,
        overlay_mode=None,
        temperature=None,
        duration=None,
        device_type="HEATING",
        mode=None,
        fan_speed=None,
        swing=None,
        fan_level=None,
        vertical_swing=None,
        horizontal_swing=None,
    ) -> None:
        """Set a zone overlay."""

        _LOGGER.debug(
            "Set overlay for zone %s: overlay_mode=%s, temp=%s, duration=%s, type=%s, mode=%s, fan_speed=%s, swing=%s, fan_level=%s, vertical_swing=%s, horizontal_swing=%s",
            zone_id,
            overlay_mode,
            temperature,
            duration,
            device_type,
            mode,
            fan_speed,
            swing,
            fan_level,
            vertical_swing,
            horizontal_swing,
        )

        try:
            await self.hass.async_add_executor_job(
                self._tado.set_zone_overlay,
                zone_id,
                overlay_mode,
                temperature,
                duration,
                device_type,
                "ON",
                mode,
                fan_speed,
                swing,
                fan_level,
                vertical_swing,
                horizontal_swing,
            )

        except RequestException as err:
            raise UpdateFailed(f"Error setting Tado overlay: {err}") from err

        await self._update_zone(zone_id)

    async def set_zone_off(self, zone_id, overlay_mode, device_type="HEATING"):
        """Set a zone to off."""
        try:
            await self.hass.async_add_executor_job(
                self._tado.set_zone_overlay,
                zone_id,
                overlay_mode,
                None,
                None,
                device_type,
                "OFF",
            )
        except RequestException as err:
            raise UpdateFailed(f"Error setting Tado overlay: {err}") from err

        await self._update_zone(zone_id)

    async def set_temperature_offset(self, device_id, offset):
        """Set temperature offset of device."""
        try:
            await self.hass.async_add_executor_job(
                self._tado.set_temp_offset, device_id, offset
            )
        except RequestException as err:
            raise UpdateFailed(f"Error setting Tado temperature offset: {err}") from err

    async def set_meter_reading(self, reading: int) -> dict[str, Any]:
        """Send meter reading to Tado."""
        dt: str = datetime.now().strftime("%Y-%m-%d")
        if self._tado is None:
            raise HomeAssistantError("Tado client is not initialized")

        try:
            return await self.hass.async_add_executor_job(
                self._tado.set_eiq_meter_readings, dt, reading
            )
        except RequestException as err:
            raise UpdateFailed(f"Error setting Tado meter reading: {err}") from err

    async def set_child_lock(self, device_id: str, enabled: bool) -> None:
        """Set child lock of device."""
        try:
            await self.hass.async_add_executor_job(
                self._tado.set_child_lock,
                device_id,
                enabled,
            )
        except RequestException as exc:
            raise HomeAssistantError(f"Error setting Tado child lock: {exc}") from exc

    def get_rate_limit(self) -> dict[str, str]:
        """Get the current rate limit status from Tado."""
        return self._tado.rate_limit_info()
