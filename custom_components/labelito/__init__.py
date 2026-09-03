# SPDX-License-Identifier: MIT
"""The labelito integration: label printing on Brother QL printers via a labelito service."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_VERIFY_SSL, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import LabelitoAuthError, LabelitoClient, LabelitoConnectionError, LabelitoSSLError
from .const import (
    CONF_API_TOKEN,
    DEFAULT_SSL,
    DEFAULT_VERIFY_SSL,
    DOMAIN,
    MAX_API_VERSION,
    MIN_API_VERSION,
)
from .coordinator import LabelitoCoordinator
from .intents import async_setup_intents
from .services import async_setup_services
from .voice_sentences import async_refresh_voice_sentences, async_setup_voice_sentences_service

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type LabelitoConfigEntry = ConfigEntry[LabelitoCoordinator]


@callback
def async_create_client(hass: HomeAssistant, data: Mapping[str, Any]) -> LabelitoClient:
    """Build a client from config-entry data, or from candidate input in the config flow.

    The single place where connection settings map onto a client, shared with the config flow so a
    probe there behaves exactly like the live entry. Certificate verification is a property of Home
    Assistant's shared sessions rather than of a request, so the session is chosen here instead of
    inside the deliberately framework-free client. Entries created before the TLS options existed
    carry neither key and fall back to plain HTTP — exactly what they were already doing.
    """
    use_ssl: bool = data.get(CONF_SSL, DEFAULT_SSL)
    # Only meaningful over https; on plain HTTP the standard shared session is used so a leftover
    # "don't verify" setting cannot quietly opt the entry into the non-verifying session.
    verify_ssl: bool = (
        data.get(CONF_VERIFY_SSL, DEFAULT_VERIFY_SSL) if use_ssl else DEFAULT_VERIFY_SSL
    )
    return LabelitoClient(
        data[CONF_HOST],
        data[CONF_PORT],
        data.get(CONF_API_TOKEN),
        async_get_clientsession(hass, verify_ssl=verify_ssl),
        use_ssl=use_ssl,
    )


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-level services and intents.

    Done here rather than in async_setup_entry so they exist exactly once regardless of how many
    entries are loaded and never disappear during an entry reload; the handlers resolve a loaded
    entry at call time (see services.resolve_coordinator).
    """
    async_setup_services(hass)
    async_setup_intents(hass)
    async_setup_voice_sentences_service(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LabelitoConfigEntry) -> bool:
    client = async_create_client(hass, entry.data)

    try:
        health = await client.health()
    except LabelitoSSLError as err:
        # Deliberately not ConfigEntryNotReady: a rejected certificate never heals on its own, so
        # retrying forever would hide a problem only the user can fix.
        raise ConfigEntryError(
            f"{err}. Fix the certificate, or turn off certificate verification for this entry "
            "(Settings -> Devices & services -> labelito -> Reconfigure)"
        ) from err
    except LabelitoConnectionError as err:
        raise ConfigEntryNotReady(f"labelito service unreachable: {err}") from err

    # Re-gate on every setup, not just in the config flow: the labelito server may have been
    # upgraded across a breaking API change since the entry was created.
    api_version = health.get("api_version")
    if not isinstance(api_version, int) or not MIN_API_VERSION <= api_version <= MAX_API_VERSION:
        raise ConfigEntryError(
            f"Unsupported labelito API version {api_version!r} "
            f"(supported: {MIN_API_VERSION}..{MAX_API_VERSION}); "
            "update the integration or the labelito service"
        )

    coordinator = LabelitoCoordinator(hass, entry, client, health)
    await coordinator.async_config_entry_first_refresh()

    # Warm the template cache so the first voice command / service call validates instantly.
    # Auth failures surface here (health and /templates are unauthenticated; /printer/status is
    # token-checked and already raised ConfigEntryAuthFailed in the first refresh if needed).
    try:
        await coordinator.async_get_templates()
    except LabelitoAuthError as err:
        raise ConfigEntryAuthFailed from err
    except LabelitoConnectionError as err:
        raise ConfigEntryNotReady(f"Could not fetch templates: {err}") from err

    # Keep an already-opted-in voice grammar current across a restart, and across templates added
    # while Home Assistant was down. Writes nothing unless labelito.write_voice_sentences has been
    # run at least once — setting up a printer must not put files in the config directory on its
    # own — and never raises, so optional voice work cannot stop a printer from loading.
    await async_refresh_voice_sentences(coordinator)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: LabelitoConfigEntry) -> None:
    # Options (poll interval) feed the coordinator constructor; a reload applies them cleanly.
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LabelitoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
