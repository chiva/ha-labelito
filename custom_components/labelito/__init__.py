# SPDX-License-Identifier: MIT
"""The Labelito integration: label printing on Brother QL printers via a labelito service."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryError, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import LabelitoAuthError, LabelitoClient, LabelitoConnectionError
from .const import CONF_API_TOKEN, DOMAIN, MAX_API_VERSION, MIN_API_VERSION
from .coordinator import LabelitoCoordinator
from .intents import async_setup_intents
from .services import async_setup_services

PLATFORMS = [Platform.BINARY_SENSOR, Platform.BUTTON, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type LabelitoConfigEntry = ConfigEntry[LabelitoCoordinator]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register domain-level services and intents.

    Done here rather than in async_setup_entry so they exist exactly once regardless of how many
    entries are loaded and never disappear during an entry reload; the handlers resolve a loaded
    entry at call time (see services.resolve_coordinator).
    """
    async_setup_services(hass)
    async_setup_intents(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LabelitoConfigEntry) -> bool:
    session = async_get_clientsession(hass)
    client = LabelitoClient(
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data.get(CONF_API_TOKEN),
        session,
    )

    try:
        health = await client.health()
    except LabelitoConnectionError as err:
        raise ConfigEntryNotReady(f"Labelito service unreachable: {err}") from err

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

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def _async_update_listener(hass: HomeAssistant, entry: LabelitoConfigEntry) -> None:
    # Options (poll interval) feed the coordinator constructor; a reload applies them cleanly.
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LabelitoConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
