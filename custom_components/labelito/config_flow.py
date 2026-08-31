# SPDX-License-Identifier: MIT
"""Config flow for labelito: manual setup, add-on (hassio) discovery, reauth, and options."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_VERIFY_SSL
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.service_info.hassio import HassioServiceInfo

from . import async_create_client
from .api import LabelitoAuthError, LabelitoConnectionError, LabelitoSSLError
from .const import (
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_SSL,
    DEFAULT_VERIFY_SSL,
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
        vol.Required(CONF_SSL, default=DEFAULT_SSL): cv.boolean,
        vol.Required(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): cv.boolean,
        vol.Optional(CONF_API_TOKEN): cv.string,
    }
)


ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_INVALID_AUTH = "invalid_auth"
ERROR_SSL_ERROR = "ssl_error"
ERROR_UNSUPPORTED_API_VERSION = "unsupported_api_version"
ERROR_UNKNOWN = "unknown"

# Reconfigure aborts when the address now answers with a different printer serial: silently
# retargeting an entry would move every existing entity onto other hardware.
ABORT_WRONG_PRINTER = "wrong_printer"

MIN_SCAN_INTERVAL_SECONDS = 5
MAX_SCAN_INTERVAL_SECONDS = 3600


def _reconfigure_schema(data: Mapping[str, Any]) -> vol.Schema:
    """Address and TLS settings of an existing entry, pre-filled with what it currently uses.

    The API token is deliberately absent: it is a secret that should not be echoed back into a
    form, and replacing it is what the reauth flow already does. Entries created before the TLS
    options existed have neither key, so both fall back to their defaults.
    """
    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=data[CONF_HOST]): cv.string,
            vol.Required(CONF_PORT, default=data[CONF_PORT]): cv.port,
            vol.Required(CONF_SSL, default=data.get(CONF_SSL, DEFAULT_SSL)): cv.boolean,
            vol.Required(
                CONF_VERIFY_SSL, default=data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL)
            ): cv.boolean,
        }
    )


class UnsupportedApiVersion(Exception):
    """The labelito server speaks an api_version outside [MIN, MAX]."""


class LabelitoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the labelito config flow."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovery: dict[str, Any] | None = None

    async def _async_validate(self, data: Mapping[str, Any]) -> dict[str, Any]:
        """Probe the service described by ``data`` and return the printer status payload.

        Goes through the same client factory as entry setup, so the scheme and certificate
        verification a user picks here are exactly what the live entry will use.

        GET /health (unauthenticated) proves reachability and gates api_version;
        GET /printer/status is token-checked, so a bad token surfaces there as 401.
        """
        client = async_create_client(self.hass, data)
        health = await client.health()
        api_version = health.get("api_version")
        if (
            not isinstance(api_version, int)
            or not MIN_API_VERSION <= api_version <= MAX_API_VERSION
        ):
            raise UnsupportedApiVersion
        return await client.printer_status()

    @staticmethod
    def _printer_serial(status: dict[str, Any]) -> str | None:
        """Printer serial from the SNMP status channel — present on network transports only."""
        serial = status.get("serial")
        return serial if isinstance(serial, str) and serial else None

    @classmethod
    def _unique_id(cls, status: dict[str, Any], data: Mapping[str, Any]) -> str:
        # USB/file deployments report no serial, so the service address is the stable fallback
        # identity. The scheme is deliberately left out of it: putting an already-configured
        # service behind TLS has to match the existing entry, not create a second one.
        return cls._printer_serial(status) or f"{data[CONF_HOST]}:{data[CONF_PORT]}"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {
                CONF_HOST: user_input[CONF_HOST],
                CONF_PORT: user_input[CONF_PORT],
                CONF_SSL: user_input[CONF_SSL],
                CONF_VERIFY_SSL: user_input[CONF_VERIFY_SSL],
                CONF_API_TOKEN: user_input.get(CONF_API_TOKEN),
            }
            # LabelitoSSLError is caught ahead of LabelitoConnectionError, its parent: a
            # certificate problem has its own remedy, and reporting it as a plain connection
            # failure would send users hunting through the network instead.
            try:
                status = await self._async_validate(data)
            except LabelitoSSLError:
                errors["base"] = ERROR_SSL_ERROR
            except LabelitoConnectionError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except LabelitoAuthError:
                errors["base"] = ERROR_INVALID_AUTH
            except UnsupportedApiVersion:
                errors["base"] = ERROR_UNSUPPORTED_API_VERSION
            else:
                await self.async_set_unique_id(self._unique_id(status, data))
                # Re-running the flow against an already-configured service adopts the new
                # address and scheme, so an http entry can be moved to https this way too.
                self._abort_if_unique_id_configured(
                    updates={
                        CONF_HOST: data[CONF_HOST],
                        CONF_PORT: data[CONF_PORT],
                        CONF_SSL: data[CONF_SSL],
                        CONF_VERIFY_SSL: data[CONF_VERIFY_SSL],
                    }
                )
                return self.async_create_entry(title=f"labelito ({data[CONF_HOST]})", data=data)
        return self.async_show_form(step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors)

    async def async_step_hassio(self, discovery_info: HassioServiceInfo) -> ConfigFlowResult:
        """Handle discovery from the labelito add-on (service name "labelito").

        The add-on publishes {host, port, api_token} so setup is one confirmation click.
        """
        config = discovery_info.config
        data = {
            CONF_HOST: config["host"],
            CONF_PORT: config["port"],
            # The add-on is reached over the Supervisor's internal network, which is plain HTTP
            # with no certificate in play. https is for externally proxied deployments only, so it
            # is pinned off here rather than defaulted.
            CONF_SSL: False,
            CONF_VERIFY_SSL: DEFAULT_VERIFY_SSL,
            CONF_API_TOKEN: config.get("api_token"),
        }
        try:
            status = await self._async_validate(data)
        except LabelitoConnectionError:
            return self.async_abort(reason=ERROR_CANNOT_CONNECT)
        except LabelitoAuthError:
            return self.async_abort(reason=ERROR_INVALID_AUTH)
        except UnsupportedApiVersion:
            return self.async_abort(reason=ERROR_UNSUPPORTED_API_VERSION)

        await self.async_set_unique_id(self._unique_id(status, data))
        self._abort_if_unique_id_configured(updates=data)
        self._discovery = data
        self.context["title_placeholders"] = {"name": discovery_info.name or "labelito"}
        return await self.async_step_hassio_confirm()

    async def async_step_hassio_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery is not None
        if user_input is not None:
            return self.async_create_entry(
                title=f"labelito ({self._discovery[CONF_HOST]})", data=self._discovery
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
                await self._async_validate({**entry.data, CONF_API_TOKEN: api_token})
            except LabelitoSSLError:
                errors["base"] = ERROR_SSL_ERROR
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

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change where the labelito service lives — address, port, and TLS — in place.

        Entries created before the TLS options existed can only reach them here, so this is what
        turns on https for an existing install without deleting and re-adding it (which would
        drop the entry's options and history). The stored API token is carried through untouched.
        """
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()
        if user_input is not None:
            data = {**entry.data, **user_input}
            try:
                status = await self._async_validate(data)
            except LabelitoSSLError:
                errors["base"] = ERROR_SSL_ERROR
            except LabelitoConnectionError:
                errors["base"] = ERROR_CANNOT_CONNECT
            except LabelitoAuthError:
                errors["base"] = ERROR_INVALID_AUTH
            except UnsupportedApiVersion:
                errors["base"] = ERROR_UNSUPPORTED_API_VERSION
            else:
                if serial := self._printer_serial(status):
                    # Hardware identity available: refuse to repoint the entry at another printer.
                    await self.async_set_unique_id(serial)
                    self._abort_if_unique_id_mismatch(reason=ABORT_WRONG_PRINTER)
                    return self.async_update_reload_and_abort(entry, data_updates=user_input)
                # No serial (USB/file transport), so identity is the address itself — a service
                # that moved takes its identity along instead of failing a mismatch check.
                return self.async_update_reload_and_abort(
                    entry, data_updates=user_input, unique_id=self._unique_id(status, data)
                )
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_reconfigure_schema(entry.data),
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
