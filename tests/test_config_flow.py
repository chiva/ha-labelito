"""Tests for the labelito config, discovery, reauth, and options flows."""

from __future__ import annotations

import ssl
from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
import voluptuous as vol
from aiohttp.client_reqrep import ConnectionKey
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SSL, CONF_VERIFY_SSL
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.hassio import HassioServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.labelito.const import (
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_VOICE_DRY_RUN,
    DOMAIN,
    SCAN_INTERVAL_USB,
)

from .conftest import register_labelito
from .const import (
    BASE_URL,
    MOCK_CONFIG,
    MOCK_CONFIG_LEGACY,
    MOCK_CONFIG_SSL,
    MOCK_HEALTH,
    MOCK_HOST,
    MOCK_PORT,
    MOCK_SERIAL,
    MOCK_STATUS,
    MOCK_TOKEN,
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


@pytest.fixture
def bypass_setup() -> Generator[None]:
    """Only exercise the flow, not the whole integration setup."""
    with patch("custom_components.labelito.async_setup_entry", return_value=True):
        yield


async def test_user_flow_success(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, bypass_setup: None
) -> None:
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == MOCK_CONFIG
    assert result["result"].unique_id == MOCK_SERIAL


async def test_user_flow_cannot_connect(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    aioclient_mock.get(f"{BASE_URL}/health", exc=aiohttp.ClientError("down"))
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_flow_invalid_auth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    register_labelito(aioclient_mock, status={"detail": "bad token"}, status_code=401)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_flow_unsupported_api_version(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    register_labelito(aioclient_mock, health={**MOCK_HEALTH, "api_version": 99})
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["errors"] == {"base": "unsupported_api_version"}


async def test_hassio_discovery_confirm(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, bypass_setup: None
) -> None:
    discovery = HassioServiceInfo(
        config={
            "host": MOCK_CONFIG[CONF_HOST],
            "port": MOCK_CONFIG[CONF_PORT],
            "api_token": MOCK_TOKEN,
        },
        name="labelito",
        slug="labelito",
        uuid="abc123",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=discovery
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hassio_confirm"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_API_TOKEN] == MOCK_TOKEN


async def test_reauth_flow_updates_token(
    hass: HomeAssistant,
    mock_labelito: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
    bypass_setup: None,
) -> None:
    """A successful reauth stores the new token.

    ``bypass_setup`` matters here: reauth ends in async_update_reload_and_abort, which schedules a
    real entry reload. Left to run, that reload races the fixture's shutdown -- it gets as far as
    registering entities, the entity registry schedules a debounced Store write, and teardown
    cancels the reload before that write is flushed, so pytest-homeassistant's verify_cleanup
    fails the teardown with a lingering timer. It reproduces on Linux and not on macOS, purely on
    event-loop timing. The reload adds nothing to what this test asserts, so it is stubbed out.
    """
    mock_config_entry.add_to_hass(hass)
    result = await mock_config_entry.start_reauth_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reauth_confirm"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={CONF_API_TOKEN: "new-token"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert mock_config_entry.data[CONF_API_TOKEN] == "new-token"
    # Let the stubbed reload settle so no task outlives the test either.
    await hass.async_block_till_done()


async def test_options_flow_updates_scan_interval(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_SCAN_INTERVAL: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == 120


async def test_options_flow_sets_voice_dry_run_without_pinning_usb_interval(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """Enabling the dry-run toggle must not freeze a USB entry's derived poll interval.

    The form now has a second, unrelated purpose, so it gets opened and saved by someone who never
    looked at the interval. A USB entry polls at the derived 90 s with no option stored; if saving
    wrote the field's value, the interval would become an explicit override — and a USB status read
    claims the single device handle, which is exactly why it is polled less often.
    """
    register_labelito(aioclient_mock, health={**MOCK_HEALTH, "transport": "usb"})
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    coordinator = mock_config_entry.runtime_data
    assert coordinator.update_interval == SCAN_INTERVAL_USB

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    # Submit the interval the form was pre-filled with — i.e. the user touched only the toggle.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            CONF_SCAN_INTERVAL: int(SCAN_INTERVAL_USB.total_seconds()),
            CONF_VOICE_DRY_RUN: True,
        },
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_VOICE_DRY_RUN] is True
    # No override stored: the interval stays derived from the transport.
    assert CONF_SCAN_INTERVAL not in mock_config_entry.options


async def test_options_flow_keeps_an_explicit_interval_that_matches_the_default(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """An interval the user DID choose must survive, even when it equals the derived value.

    The drop above is keyed on the entry having had no override, so an explicit 30 s on a network
    entry is not mistaken for "untouched" and silently removed.
    """
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={CONF_SCAN_INTERVAL: 30})
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_SCAN_INTERVAL: 30, CONF_VOICE_DRY_RUN: False}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == 30


async def test_user_flow_over_https(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bypass_setup: None
) -> None:
    """Ticking "Use HTTPS" has to make the probe — and the stored entry — use https."""
    register_labelito(aioclient_mock, base_url=SSL_BASE_URL)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_SSL
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == MOCK_CONFIG_SSL
    assert str(aioclient_mock.mock_calls[0][1]).startswith(SSL_BASE_URL)


async def test_user_flow_without_ssl_stays_on_plain_http(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, bypass_setup: None
) -> None:
    """The counterpart guard: leaving the box unticked must not reach for https."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SSL] is False
    assert str(mock_labelito.mock_calls[0][1]).startswith(BASE_URL)


async def test_user_flow_reports_certificate_error_distinctly(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """A rejected certificate must not read as a generic connection failure."""
    aioclient_mock.get(
        f"{SSL_BASE_URL}/health",
        exc=aiohttp.ClientConnectorCertificateError(
            _CONNECTION_KEY, ssl.SSLCertVerificationError("self-signed")
        ),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=MOCK_CONFIG_SSL
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "ssl_error"}


async def test_verify_ssl_off_selects_the_non_verifying_session(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, bypass_setup: None
) -> None:
    """Certificate verification is a property of the shared session, so assert on its selection."""
    register_labelito(aioclient_mock, base_url=SSL_BASE_URL)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.labelito.async_get_clientsession",
        wraps=async_get_clientsession,
    ) as get_session:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={**MOCK_CONFIG_SSL, CONF_VERIFY_SSL: False},
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert get_session.call_args.kwargs["verify_ssl"] is False


async def test_plain_http_entry_keeps_the_verifying_session(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, bypass_setup: None
) -> None:
    """A stale "don't verify" flag must not pull an http entry onto the unverified session."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.labelito.async_get_clientsession",
        wraps=async_get_clientsession,
    ) as get_session:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={**MOCK_CONFIG, CONF_VERIFY_SSL: False}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert get_session.call_args.kwargs["verify_ssl"] is True


async def test_hassio_discovery_pins_plain_http(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, bypass_setup: None
) -> None:
    """The add-on is reached over the Supervisor network, which has no TLS in play."""
    discovery = HassioServiceInfo(
        config={"host": MOCK_HOST, "port": MOCK_PORT, "api_token": MOCK_TOKEN},
        name="labelito",
        slug="labelito",
        uuid="abc123",
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_HASSIO}, data=discovery
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input={})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_SSL] is False


async def test_reconfigure_switches_an_existing_entry_to_https(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """The path an already-installed user takes to reach https, without re-adding the entry."""
    register_labelito(aioclient_mock, base_url=SSL_BASE_URL)
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_HOST: MOCK_HOST,
            CONF_PORT: MOCK_PORT,
            CONF_SSL: True,
            CONF_VERIFY_SSL: False,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert mock_config_entry.data[CONF_SSL] is True
    assert mock_config_entry.data[CONF_VERIFY_SSL] is False
    # The token is not in the reconfigure form and must survive untouched.
    assert mock_config_entry.data[CONF_API_TOKEN] == MOCK_TOKEN


async def test_reconfigure_rejects_a_different_printer(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """Repointing an entry at other hardware would silently move every entity onto it."""
    register_labelito(
        aioclient_mock,
        status={**MOCK_STATUS, "serial": "SN9999999999"},
        base_url=SSL_BASE_URL,
    )
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_HOST: MOCK_HOST,
            CONF_PORT: MOCK_PORT,
            CONF_SSL: True,
            CONF_VERIFY_SSL: True,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_printer"
    assert mock_config_entry.data[CONF_SSL] is False


async def test_reconfigure_reports_certificate_error(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    aioclient_mock.get(
        f"{SSL_BASE_URL}/health",
        exc=aiohttp.ClientConnectorCertificateError(
            _CONNECTION_KEY, ssl.SSLCertVerificationError("self-signed")
        ),
    )
    mock_config_entry.add_to_hass(hass)

    result = await mock_config_entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_HOST: MOCK_HOST,
            CONF_PORT: MOCK_PORT,
            CONF_SSL: True,
            CONF_VERIFY_SSL: True,
        },
    )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "ssl_error"}


async def test_reconfigure_carries_identity_when_printer_has_no_serial(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """USB/file deployments identify by address, so a moved service keeps its entry."""
    moved_host = "192.0.2.99"
    moved_base_url = f"http://{moved_host}:{MOCK_PORT}"
    register_labelito(
        aioclient_mock,
        status={**MOCK_STATUS, "serial": None},
        base_url=moved_base_url,
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=MOCK_CONFIG,
        unique_id=f"{MOCK_HOST}:{MOCK_PORT}",
        title=f"labelito ({MOCK_HOST})",
    )
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_HOST: moved_host,
            CONF_PORT: MOCK_PORT,
            CONF_SSL: False,
            CONF_VERIFY_SSL: True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_HOST] == moved_host
    assert entry.unique_id == f"{moved_host}:{MOCK_PORT}"


async def test_reconfigure_of_entry_predating_the_tls_options(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker
) -> None:
    """The upgrade path for existing installs: no ssl keys stored, form still opens and applies."""
    register_labelito(aioclient_mock, base_url=SSL_BASE_URL)
    entry = MockConfigEntry(domain=DOMAIN, data=MOCK_CONFIG_LEGACY, unique_id=MOCK_SERIAL)
    entry.add_to_hass(hass)

    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            CONF_HOST: MOCK_HOST,
            CONF_PORT: MOCK_PORT,
            CONF_SSL: True,
            CONF_VERIFY_SSL: True,
        },
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_SSL] is True
    assert entry.data[CONF_API_TOKEN] == MOCK_TOKEN


def _schema_default(schema: vol.Schema, key: str) -> Any:
    """The value an options form pre-fills a field with (what a user submits if they don't touch it)."""
    for marker in schema.schema:
        if marker == key:
            default = marker.default
            return default() if callable(default) else default
    raise AssertionError(f"{key!r} is not in the form schema")


async def test_options_flow_on_unloaded_entry_keeps_its_scan_interval(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry
) -> None:
    """An explicit poll interval must survive saving the form while the entry is NOT_LOADED.

    Home Assistant opens the options flow for an unloaded entry too — a labelito service that is
    down puts the entry in SETUP_RETRY, which is *exactly* when someone might come here to turn on
    the dry-run toggle. There is no coordinator to read the effective interval from then, so
    pre-filling the transport default would show 30 s; submitting the form replaces the whole
    options dict, so a stored 120 would be silently rewritten to 30. The stored override therefore
    outranks every other source for the pre-filled value.
    """
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={CONF_SCAN_INTERVAL: 120})
    # Deliberately not set up: no runtime_data, so _effective_scan_interval has no coordinator.
    assert mock_config_entry.state is config_entries.ConfigEntryState.NOT_LOADED

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    assert _schema_default(result["data_schema"], CONF_SCAN_INTERVAL) == 120

    # Submit what the form offered — i.e. the user touched only the toggle.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_SCAN_INTERVAL: 120, CONF_VOICE_DRY_RUN: True},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == 120
    assert mock_config_entry.options[CONF_VOICE_DRY_RUN] is True


async def test_options_flow_preserves_options_outside_the_form(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """Saving the form must not delete an option it does not carry.

    async_create_entry replaces the options dict wholesale, so the submitted input is merged over
    what is stored. Nothing lives outside the schema today; this pins the behaviour before the next
    field added conditionally silently drops its neighbours.
    """
    mock_config_entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(mock_config_entry, options={"future_option": "keep me"})
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(mock_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={CONF_SCAN_INTERVAL: 45, CONF_VOICE_DRY_RUN: True}
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert mock_config_entry.options["future_option"] == "keep me"
    assert mock_config_entry.options[CONF_SCAN_INTERVAL] == 45
