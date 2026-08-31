"""Tests for integration setup/unload and the API-version gate in __init__.py."""

from __future__ import annotations

import ssl

import aiohttp
import pytest
from aiohttp.client_reqrep import ConnectionKey
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.labelito.const import DOMAIN

from .conftest import register_labelito
from .const import (
    BASE_URL,
    MOCK_CONFIG_LEGACY,
    MOCK_CONFIG_SSL,
    MOCK_HEALTH,
    MOCK_HOST,
    MOCK_PORT,
    MOCK_SERIAL,
    SSL_BASE_URL,
)

# See tests/test_api.py: aiohttp's TLS errors render their message from the connection key.
_CONNECTION_KEY = ConnectionKey(
    host=MOCK_HOST,
    port=MOCK_PORT,
    is_ssl=True,
    ssl=True,
    proxy=None,
    proxy_auth=None,
    proxy_headers_hash=None,
)


async def test_setup_and_unload(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert mock_config_entry.state is ConfigEntryState.LOADED
    assert isinstance(mock_config_entry.runtime_data, object)
    # Domain services are registered once at setup.
    assert hass.services.has_service(DOMAIN, "print")
    assert hass.services.has_service(DOMAIN, "reprint_last")

    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.NOT_LOADED


async def test_setup_retry_when_service_unreachable(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    aioclient_mock.get(f"{BASE_URL}/health", exc=aiohttp.ClientError("down"))
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_RETRY


# The gate is pinned to v3, the sole supported contract.
async def test_setup_accepts_supported_api_version(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    register_labelito(aioclient_mock, health={**MOCK_HEALTH, "api_version": 3})
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.LOADED


# Superseded older contracts (1, 2), the next breaking bump (4), and a far-future one are rejected.
@pytest.mark.parametrize("api_version", [1, 2, 4, 99])
async def test_setup_error_on_unsupported_api_version(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    api_version: int,
) -> None:
    register_labelito(aioclient_mock, health={**MOCK_HEALTH, "api_version": api_version})
    mock_config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    assert mock_config_entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_over_https(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """An entry with ssl on talks to https for its whole lifecycle, not just in the config flow."""
    register_labelito(aioclient_mock, base_url=SSL_BASE_URL)
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_SSL, unique_id=MOCK_SERIAL)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert all(str(call[1]).startswith(SSL_BASE_URL) for call in aioclient_mock.mock_calls)


async def test_setup_error_not_retry_on_certificate_failure(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A rejected certificate never heals by itself, so SETUP_RETRY would hide it forever."""
    aioclient_mock.get(
        f"{SSL_BASE_URL}/health",
        exc=aiohttp.ClientConnectorCertificateError(
            _CONNECTION_KEY, ssl.SSLCertVerificationError("self-signed")
        ),
    )
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_SSL, unique_id=MOCK_SERIAL)
    entry.add_to_hass(hass)

    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR


async def test_setup_of_entry_predating_the_tls_options(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker
) -> None:
    """Backwards compatibility: an entry with no ssl keys must still load over plain HTTP."""
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_LEGACY, unique_id=MOCK_SERIAL)
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert all(str(call[1]).startswith(BASE_URL) for call in mock_labelito.mock_calls)
