"""Tests for the async labelito HTTP client (custom_components/labelito/api.py)."""

from __future__ import annotations

import aiohttp
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.labelito.api import (
    PRINT_TIMEOUT_SECONDS,
    SEQUENCE_PER_LABEL_TIMEOUT_SECONDS,
    LabelitoApiError,
    LabelitoAuthError,
    LabelitoClient,
    LabelitoConnectionError,
    _print_timeout,
)

from .const import (
    BASE_URL,
    MOCK_HEALTH,
    MOCK_HOST,
    MOCK_PORT,
    MOCK_TEMPLATES,
    MOCK_TOKEN,
)


def _client(hass: HomeAssistant, token: str | None = MOCK_TOKEN) -> LabelitoClient:
    return LabelitoClient(MOCK_HOST, MOCK_PORT, token, async_get_clientsession(hass))


async def test_health_ok(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE_URL}/health", json=MOCK_HEALTH)
    assert (await _client(hass).health())["api_version"] == 3


async def test_bearer_token_sent(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    aioclient_mock.get(f"{BASE_URL}/health", json=MOCK_HEALTH)
    await _client(hass).health()
    headers = aioclient_mock.mock_calls[0][3]
    assert headers["Authorization"] == f"Bearer {MOCK_TOKEN}"


async def test_no_token_sends_no_auth_header(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE_URL}/health", json=MOCK_HEALTH)
    await _client(hass, token=None).health()
    assert "Authorization" not in (aioclient_mock.mock_calls[0][3] or {})


async def test_printer_status_treats_503_as_data(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 503 from /printer/status carries a full body (unreachable printer), not an error."""
    body = {"reachable": False, "state": "off"}
    aioclient_mock.get(f"{BASE_URL}/printer/status", json=body, status=503)
    assert (await _client(hass).printer_status())["reachable"] is False


async def test_auth_error_raised_on_401(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE_URL}/printer/status", json={"detail": "nope"}, status=401)
    with pytest.raises(LabelitoAuthError):
        await _client(hass).printer_status()


async def test_connection_error_on_client_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE_URL}/health", exc=aiohttp.ClientError("boom"))
    with pytest.raises(LabelitoConnectionError):
        await _client(hass).health()


async def test_api_error_preserves_status_and_dict_detail(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A 409 media mismatch surfaces the structured FastAPI detail dict verbatim."""
    detail = {
        "msg": "media mismatch",
        "media_required": "29x90mm die-cut",
        "media_loaded": "62mm continuous",
    }
    aioclient_mock.post(f"{BASE_URL}/print", json={"detail": detail}, status=409)
    with pytest.raises(LabelitoApiError) as err:
        await _client(hass).print_label({"template": "pantry", "fields": {}})
    assert err.value.status == 409
    assert err.value.detail == detail


async def test_api_error_falls_back_to_text_for_non_json_body(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A non-JSON error body (e.g. the body-size middleware's plain 413) surfaces as raw text.

    Exercises _extract_detail's fallback branch: response.json() raises on a non-JSON body, so
    the handler returns response.text() rather than propagating the decode error.
    """
    aioclient_mock.post(f"{BASE_URL}/print", text="Request Entity Too Large", status=413)
    with pytest.raises(LabelitoApiError) as err:
        await _client(hass).print_label({"template": "pantry", "fields": {}})
    assert err.value.status == 413
    assert err.value.detail == "Request Entity Too Large"


async def test_api_error_returns_json_body_verbatim_without_detail_key(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A JSON error body that is not the FastAPI ``{"detail": ...}`` envelope is surfaced as-is."""
    body = {"error": "unclassified", "code": 42}
    aioclient_mock.post(f"{BASE_URL}/print", json=body, status=400)
    with pytest.raises(LabelitoApiError) as err:
        await _client(hass).print_label({"template": "pantry", "fields": {}})
    assert err.value.status == 400
    assert err.value.detail == body


async def test_templates_returns_list(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE_URL}/templates", json=MOCK_TEMPLATES)
    names = [t["name"] for t in await _client(hass).templates()]
    assert names == ["pantry", "freezer", "crate"]


def test_print_timeout_is_base_for_single_label() -> None:
    assert _print_timeout(1).total == PRINT_TIMEOUT_SECONDS


def test_print_timeout_scales_with_sequence_size() -> None:
    # A large batch must not false-timeout: the budget grows per label beyond the first.
    assert (
        _print_timeout(500).total
        == PRINT_TIMEOUT_SECONDS + 499 * SEQUENCE_PER_LABEL_TIMEOUT_SECONDS
    )


async def test_reprint_posts_job_path(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.post(f"{BASE_URL}/reprint/job-1", json={"job_id": "job-1", "dry_run": False})
    assert (await _client(hass).reprint("job-1"))["job_id"] == "job-1"
