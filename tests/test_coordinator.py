"""Tests for LabelitoCoordinator polling and the TTL template cache."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.labelito.api import (
    LabelitoAuthError,
    LabelitoClient,
    LabelitoConnectionError,
    LabelitoError,
)
from custom_components.labelito.coordinator import LabelitoCoordinator

from .const import BASE_URL, MOCK_HEALTH, MOCK_STATUS, MOCK_TEMPLATES


def _coordinator(
    hass: HomeAssistant, entry: MockConfigEntry, client: AsyncMock
) -> LabelitoCoordinator:
    entry.add_to_hass(hass)
    return LabelitoCoordinator(hass, entry, client, MOCK_HEALTH)


@pytest.fixture
def client() -> AsyncMock:
    mock = AsyncMock(spec=LabelitoClient)
    mock.base_url = BASE_URL
    mock.printer_status.return_value = MOCK_STATUS
    mock.templates.return_value = list(MOCK_TEMPLATES)
    return mock


async def test_update_returns_status(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    coord = _coordinator(hass, mock_config_entry, client)
    assert (await coord._async_update_data())["state"] == "idle"


async def test_unreachable_printer_503_stays_healthy(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    """A 503-body (unreachable printer) is data, not a coordinator failure."""
    client.printer_status.return_value = {"reachable": False, "state": "off"}
    coord = _coordinator(hass, mock_config_entry, client)
    assert (await coord._async_update_data())["reachable"] is False


async def test_service_unreachable_raises_update_failed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.printer_status.side_effect = LabelitoConnectionError("down")
    coord = _coordinator(hass, mock_config_entry, client)
    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


async def test_auth_failure_raises_config_entry_auth_failed(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.printer_status.side_effect = LabelitoAuthError("bad token")
    coord = _coordinator(hass, mock_config_entry, client)
    with pytest.raises(ConfigEntryAuthFailed):
        await coord._async_update_data()


async def test_template_cache_hits_within_ttl(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    coord = _coordinator(hass, mock_config_entry, client)
    await coord.async_get_templates()
    await coord.async_get_templates()
    assert client.templates.await_count == 1


async def test_force_refresh_refetches(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    coord = _coordinator(hass, mock_config_entry, client)
    await coord.async_get_templates()
    await coord.async_get_templates(force_refresh=True)
    assert client.templates.await_count == 2


async def test_stale_catalog_served_on_refresh_error(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    """After a warm cache, a failed refresh serves the stale catalog instead of raising."""
    coord = _coordinator(hass, mock_config_entry, client)
    await coord.async_get_templates()
    client.templates.side_effect = LabelitoError("blip")
    result = await coord.async_get_templates(force_refresh=True)
    assert coord.template_names(result) == ["freezer", "pantry"]


async def test_initial_template_fetch_error_propagates(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    client.templates.side_effect = LabelitoError("cold")
    coord = _coordinator(hass, mock_config_entry, client)
    with pytest.raises(LabelitoError):
        await coord.async_get_templates()


async def test_cached_template_count_falls_back_to_health(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> None:
    coord = _coordinator(hass, mock_config_entry, client)
    assert coord.cached_template_count == MOCK_HEALTH["template_count"]
    await coord.async_get_templates()
    assert coord.cached_template_count == len(MOCK_TEMPLATES)
