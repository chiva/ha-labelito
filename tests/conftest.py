"""Global fixtures for the Labelito integration tests."""

from __future__ import annotations

from collections.abc import Generator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.labelito.const import DOMAIN

from .const import (
    BASE_URL,
    MOCK_CONFIG,
    MOCK_HEALTH,
    MOCK_SERIAL,
    MOCK_STATUS,
    MOCK_TEMPLATES,
)

pytest_plugins = "pytest_homeassistant_custom_component"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    enable_custom_integrations: None,
) -> Generator[None]:
    """Load custom_components/labelito in every test."""
    yield


def register_labelito(
    aioclient_mock: AiohttpClientMocker,
    *,
    health: dict | None = None,
    health_status: int = 200,
    status: dict | None = None,
    status_code: int = 200,
    templates: list | None = None,
) -> None:
    """Register the read endpoints the config flow and coordinator hit.

    Defaults describe a healthy, reachable network printer; pass overrides to drive error paths.
    """
    aioclient_mock.get(
        f"{BASE_URL}/health",
        json=health if health is not None else MOCK_HEALTH,
        status=health_status,
    )
    aioclient_mock.get(
        f"{BASE_URL}/printer/status",
        json=status if status is not None else MOCK_STATUS,
        status=status_code,
    )
    aioclient_mock.get(
        f"{BASE_URL}/templates",
        json=templates if templates is not None else MOCK_TEMPLATES,
    )


@pytest.fixture
def mock_labelito(aioclient_mock: AiohttpClientMocker) -> AiohttpClientMocker:
    """A healthy labelito service on all read endpoints."""
    register_labelito(aioclient_mock)
    return aioclient_mock


@pytest.fixture
def mock_config_entry() -> MockConfigEntry:
    """A config entry as the user/discovery flow would have created it."""
    return MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        unique_id=MOCK_SERIAL,
        title="Labelito (192.0.2.10)",
    )
