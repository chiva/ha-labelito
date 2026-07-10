# SPDX-License-Identifier: MIT
"""DataUpdateCoordinator polling labelito's /printer/status, plus a TTL template cache."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import LabelitoAuthError, LabelitoClient, LabelitoConnectionError, LabelitoError
from .const import (
    CONF_SCAN_INTERVAL,
    DOMAIN,
    SCAN_INTERVAL_NETWORK,
    SCAN_INTERVAL_USB,
    TEMPLATE_CACHE_TTL,
    TRANSPORT_NETWORK,
)

_LOGGER = logging.getLogger(__name__)


class LabelitoCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the printer status and caches the template catalog.

    ``data`` is the raw PrinterStatusResponse dict. A 503 from /printer/status still carries a
    full body (``reachable: false``), so an unreachable *printer* keeps the coordinator healthy —
    only an unreachable *labelito service* marks entities unavailable via UpdateFailed.

    ``ha_printed_count`` backs the fallback labels-printed sensor (USB/file deployments with no
    SNMP odometer). It is seeded from that sensor's restored state on add-to-hass and counts only
    labels printed *through this Home Assistant instance*, not the printer's own lifetime total.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: LabelitoClient,
        health: dict[str, Any],
    ) -> None:
        transport = health.get("transport")
        default_interval = (
            SCAN_INTERVAL_NETWORK if transport == TRANSPORT_NETWORK else SCAN_INTERVAL_USB
        )
        override_seconds: int | None = entry.options.get(CONF_SCAN_INTERVAL)
        interval = (
            default_interval if override_seconds is None else timedelta(seconds=override_seconds)
        )
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.base_url}",
            update_interval=interval,
        )
        self.client = client
        self.health = health
        # job_id of the last successful print issued through this integration; feeds the
        # reprint-last button/service. In-memory only — it resets on a Home Assistant restart.
        self.last_job_id: str | None = None
        # Physical label count of that last job (sequence batch size, else copies). Set alongside
        # last_job_id so reprint-last can credit the fallback counter correctly and scale its
        # timeout — a PrintResponse carries no item count for a replayed sequence batch.
        self.last_job_labels: int = 1
        # See the class docstring; touched only by services.py and the fallback sensor.
        self.ha_printed_count: int = 0
        # job_ids already added to ha_printed_count, so an idempotency-key replay (labelito returns
        # the prior job_id with no physical print) cannot double-count within a run. In-memory,
        # bounded by the prints issued per HA run. It intentionally resets on restart while
        # ha_printed_count is restored, so replaying the SAME stable key across a restart could add
        # one spurious count — an accepted trade-off: that retry pattern is vanishingly rare, and
        # persisting the set (or a labelito response replay-flag) is disproportionate for a
        # best-effort counter that already cannot see prints made outside Home Assistant.
        self.counted_job_ids: set[str] = set()
        self._templates: list[dict[str, Any]] | None = None
        self._templates_fetched_at: float = 0.0
        self._templates_lock = asyncio.Lock()

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            data = await self.client.printer_status()
        except LabelitoAuthError as err:
            raise ConfigEntryAuthFailed("API token rejected by labelito") from err
        except LabelitoConnectionError as err:
            raise UpdateFailed(f"labelito service unreachable: {err}") from err
        self._maybe_refresh_templates()
        return data

    def _maybe_refresh_templates(self) -> None:
        """Fire-and-forget a template-cache refresh once its TTL has expired.

        Piggybacks on the status poll instead of a dedicated timer, so the print-validation
        catalog stays warm without an extra scheduled task. Only fires once the cache has been
        populated at least once — the initial fetch is awaited explicitly in async_setup_entry,
        whose error handling (ConfigEntryNotReady/ConfigEntryAuthFailed) must not race a
        background task. The lock inside async_get_templates serializes against a concurrent
        explicit refresh, so this can never trigger a duplicate fetch.
        """
        if self._templates is None:
            return
        age = time.monotonic() - self._templates_fetched_at
        if age >= TEMPLATE_CACHE_TTL.total_seconds():
            self.hass.async_create_task(
                self.async_get_templates(), name="labelito template cache refresh"
            )

    async def async_get_templates(self, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Return the template catalog, refreshing it after TTL expiry or on demand.

        Callers force a refresh on a print-validation miss so a template added on the server is
        picked up immediately instead of after the TTL.
        """
        async with self._templates_lock:
            age = time.monotonic() - self._templates_fetched_at
            if (
                not force_refresh
                and self._templates is not None
                and age < TEMPLATE_CACHE_TTL.total_seconds()
            ):
                return self._templates
            try:
                self._templates = await self.client.templates()
            except LabelitoError as err:
                if self._templates is None:
                    raise
                # Serve the stale catalog rather than fail a print over a template listing blip.
                # Base LabelitoError, not just the connection variant: a 5xx must not escape the
                # fire-and-forget background refresh in _maybe_refresh_templates either.
                _LOGGER.warning("Template refresh failed (%s); using cached catalog", err)
                return self._templates
            self._templates_fetched_at = time.monotonic()
            return self._templates

    def template_names(self, templates: list[dict[str, Any]]) -> list[str]:
        return sorted(t["name"] for t in templates)

    @property
    def cached_template_count(self) -> int | None:
        """Template count from the warm cache, falling back to the setup-time /health value."""
        if self._templates is not None:
            return len(self._templates)
        return self.health.get("template_count")
