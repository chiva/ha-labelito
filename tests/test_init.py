"""Tests for integration setup/unload and the API-version gate in __init__.py."""

from __future__ import annotations

import aiohttp
import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.labelito.const import DOMAIN

from .conftest import register_labelito
from .const import BASE_URL, MOCK_HEALTH


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
