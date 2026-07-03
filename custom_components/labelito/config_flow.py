# SPDX-License-Identifier: MIT
"""Config flow for Labelito: manual setup, add-on (hassio) discovery, reauth, and options."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from .api import LabelitoAuthError, LabelitoClient, LabelitoConnectionError
from .const import (
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DOMAIN,
    MAX_API_VERSION,
    MIN_API_VERSION,
    SCAN_INTERVAL_NETWORK,
    SCAN_INTERVAL_USB,
)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_API_TOKEN): cv.string,
    }
)

ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_INVALID_AUTH = "invalid_auth"
ERROR_UNSUPPORTED_API_VERSION = "unsupported_api_version"
ERROR_UNKNOWN = "unknown"

MIN_SCAN_INTERVAL_SECONDS = 5
MAX_SCAN_INTERVAL_SECONDS = 3600


class UnsupportedApiVersion(Exception):
    """The labelito server speaks an api_version outside [MIN, MAX]."""


class LabelitoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Labelito config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: dict[str, Any] | None = None

    async def _async_validate(self, host: str, port: int, api_token: str | None) -> dict[str, Any]:
        """Probe the service and return the printer status payload.

        GET /health (unauthenticated) proves reachability and gates api_version;
        GET /printer/status is token-checked, so a bad token surfaces there as 401.
        """
        client = LabelitoClient(host, port, api_token, async_get_clientsession(self.hass))
        health = await client.health()
        api_version = health.get("api_version")
        if (
            not isinstance(api_version, int)
            or not MIN_API_VERSION <= api_version <= MAX_API_VERSION
        ):
            raise UnsupportedApiVersion
        return await client.printer_status()

    @staticmethod
    def _unique_id(status: dict[str, Any], host: str, port: int) -> str:
        # The SNMP status channel exposes the printer serial on network transports; USB/file
        # deployments report no serial, so the service address is the stable fallback identity.
        serial = status.get("serial")
        return serial if isinstance(serial, str) and serial else f"{host}:{port}"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host: str = user_input[CONF_HOST]
            port: int = user_input[CONF_PORT]
            api_token: str | None = user_input.get(CONF_API_TOKEN)
            try:
                status = await self._async_validate(host, port, api_token)
            except LabelitoConnectionError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except LabelitoAuthError:
                errors["base"] = ERROR_INVALID_AUTH
            except UnsupportedApiVersion:
                errors["base"] = ERROR_UNSUPPORTED_API_VERSION
            else:
                await self.async_set_unique_id(self._unique_id(status, host, port))
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                return self.async_create_entry(
                    title=f"Labelito ({host})",
                    data={CONF_HOST: host, CONF_PORT: port, CONF_API_TOKEN: api_token},
                )
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Handle discovery from the labelito add-on (service name "labelito").

        The add-on publishes {host, port, api_token} so setup is one confirmation click.
        """
        config = discovery_info.config
        host: str = config["host"]
        port: int = config["port"]
        api_token: str | None = config.get("api_token")
        try:
            status = await self._async_validate(host, port, api_token)
        except LabelitoConnectionError:
            return self.async_abort(reason=ERROR_CANNOT_CONNECT)
        except LabelitoAuthError:
            return self.async_abort(reason=ERROR_INVALID_AUTH)
        except UnsupportedApiVersion:
            return self.async_abort(reason=ERROR_UNSUPPORTED_API_VERSION)

        await self.async_set_unique_id(self._unique_id(status, host, port))
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: host, CONF_PORT: port, CONF_API_TOKEN: api_token}
        )
        self._discovery = {CONF_HOST: host, CONF_PORT: port, CONF_API_TOKEN: api_token}
        self.context["title_placeholders"] = {"name": discovery_info.name or "Labelito"}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        if user_input is not None:
            return self.async_create_entry(
                title=f"Labelito ({self._discovery[CONF_HOST]})", data=self._discovery
            )
        return self.async_show_form(
            step_id="hassio_confirm",
            description_placeholders={"host": self._discovery[CONF_HOST]},
        )

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_token: str = user_input[CONF_API_TOKEN]
            try:
                await self._async_validate(entry.data[CONF_HOST], entry.data[CONF_PORT], api_token)
            except LabelitoConnectionError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except LabelitoAuthError:
                errors["base"] = ERROR_INVALID_AUTH
            except UnsupportedApiVersion:
                errors["base"] = ERROR_UNSUPPORTED_API_VERSION
            else:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_TOKEN: api_token}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_API_TOKEN): cv.string}),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> LabelitoOptionsFlow:
        return LabelitoOptionsFlow()


class LabelitoOptionsFlow(OptionsFlow):
    """Options: override the transport-derived poll interval."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        current = self.config_entry.options.get(
            CONF_SCAN_INTERVAL, int(SCAN_INTERVAL_NETWORK.total_seconds())
        )
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): vol.All(
                    vol.Coerce(int),
                    vol.Range(min=MIN_SCAN_INTERVAL_SECONDS, max=MAX_SCAN_INTERVAL_SECONDS),
                ),
            }
        )
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "network_default": str(int(SCAN_INTERVAL_NETWORK.total_seconds())),
                "usb_default": str(int(SCAN_INTERVAL_USB.total_seconds())),
            },
        )
