"""Tests for the config-entry diagnostics payload.

diagnostics.py had no test at all: 11 statements, 0% covered. It is also the one module whose
output users paste into bug reports, so the two things worth pinning are that it carries the live
state and that it never carries the API token.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

from homeassistant.components.diagnostics import REDACTED
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.labelito.api import LabelitoError
from custom_components.labelito.const import (
    CONF_API_TOKEN,
    CONF_SCAN_INTERVAL,
    CONF_VOICE_DRY_RUN,
    TEMPLATE_CACHE_TTL,
)
from custom_components.labelito.diagnostics import async_get_config_entry_diagnostics

from .const import MOCK_HOST, MOCK_PORT, MOCK_TOKEN


def _strings(value: Any) -> list[str]:
    """Every string anywhere in a nested payload — keys included, at any depth.

    Collected rather than compared here so the caller can test for CONTAINMENT. An exact
    membership test (`MOCK_TOKEN not in _strings(payload)`) was the first version and it was
    porous: a token embedded in a larger value, say a URL with `?token=...`, is its own distinct
    string and passes such a check untouched. Verified by embedding one — all four tests stayed
    green while the secret sat in the payload in cleartext.
    """
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for item in value.items() for v in item for s in _strings(v)]
    if isinstance(value, (list, tuple, set)):
        return [s for v in value for s in _strings(v)]
    return []


async def _loaded(
    hass: HomeAssistant, entry: MockConfigEntry, *, options: dict[str, Any] | None = None
) -> MockConfigEntry:
    """Set the entry up the way Home Assistant does, so diagnostics run against a real
    runtime_data coordinator rather than a hand-built one."""
    entry.add_to_hass(hass)
    if options is not None:
        hass.config_entries.async_update_entry(entry, options=options)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_the_api_token_never_appears_in_the_payload(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """The whole payload is searched for the token as a substring, not just the key it is stored
    under.

    Asserting `data[CONF_API_TOKEN] == REDACTED` alone would keep passing if a later change
    copied the token somewhere else in the payload, which is the failure that actually matters —
    and a diagnostics payload that grew a connection URL or an error message is exactly how that
    happens, which is why this looks inside each string rather than at whole values.
    """
    entry = await _loaded(hass, mock_config_entry, options={CONF_SCAN_INTERVAL: 30})

    payload = await async_get_config_entry_diagnostics(hass, entry)

    leaked = [text for text in _strings(payload) if MOCK_TOKEN in text]
    assert not leaked, f"the API token appears in the diagnostics payload: {leaked}"
    assert payload["entry"]["data"][CONF_API_TOKEN] == REDACTED
    # Redaction replaces the secret and nothing else: the connection details a bug report needs
    # have to survive, or the redaction has made the diagnostics useless instead of safe.
    assert payload["entry"]["data"]["host"] == MOCK_HOST
    assert payload["entry"]["data"]["port"] == MOCK_PORT


async def test_diagnostics_reports_the_live_coordinator_state(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """The payload has to be a snapshot of the running integration, not of its config: health,
    last poll, template names and both counters are what makes a bug report actionable."""
    entry = await _loaded(hass, mock_config_entry)
    coordinator = entry.runtime_data
    coordinator.last_job_id = "job-42"
    coordinator.ha_printed_count = 7

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["health"] == coordinator.health
    assert payload["printer_status"] == coordinator.data
    # Names rather than the raw catalog: the same list the voice handler reads back to the user.
    assert payload["templates"] == ["crate", "freezer", "pantry"]
    assert payload["last_job_id"] == "job-42"
    assert payload["ha_printed_count"] == 7


async def test_options_are_reported_verbatim(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """Options hold no secret today (only scan_interval and voice_dry_run), which is why
    diagnostics.py redacts `data` and not `options`. The token search in the first test is what
    keeps that assumption honest if a secret ever moves here."""
    entry = await _loaded(
        hass,
        mock_config_entry,
        options={CONF_SCAN_INTERVAL: 45, CONF_VOICE_DRY_RUN: True},
    )

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["entry"]["options"] == {CONF_SCAN_INTERVAL: 45, CONF_VOICE_DRY_RUN: True}


async def test_diagnostics_still_reports_when_the_catalog_refresh_fails(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """A service that has gone unreachable is exactly when diagnostics get collected, so it must
    degrade to the cached catalog rather than fail the download.

    The cache is expired by hand: setup warms it, and within the TTL async_get_templates returns
    it without a network call, so leaving the timestamp alone would test nothing about the error
    path. (A cold cache cannot happen here — setup fails outright if the first fetch does.)

    The timestamp is set RELATIVE to the clock, not to zero. `time.monotonic()` is time since
    boot on Linux, so on a freshly started CI runner it can be smaller than the 15-minute TTL —
    an absolute 0 then reads as "fetched moments ago", the cache is served, and the error path
    never runs. That is precisely what happened: the templates assertion below passed anyway off
    the warm cache, and only the awaited-once check caught that the test was measuring nothing.
    """
    entry = await _loaded(hass, mock_config_entry)
    coordinator = entry.runtime_data
    coordinator._templates_fetched_at = time.monotonic() - TEMPLATE_CACHE_TTL.total_seconds() - 1
    coordinator.client.templates = AsyncMock(side_effect=LabelitoError("service down"))

    payload = await async_get_config_entry_diagnostics(hass, entry)

    assert payload["templates"] == ["crate", "freezer", "pantry"]
    coordinator.client.templates.assert_awaited_once()
