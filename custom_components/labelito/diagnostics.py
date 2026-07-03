# SPDX-License-Identifier: MIT
"""Diagnostics support: config entry + last known state, with the API token redacted."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import LabelitoConfigEntry
from .const import CONF_API_TOKEN

TO_REDACT = {CONF_API_TOKEN}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LabelitoConfigEntry
) -> dict[str, Any]:
    coordinator = entry.runtime_data
    templates = await coordinator.async_get_templates()
    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "health": coordinator.health,
        "printer_status": coordinator.data,
        "templates": coordinator.template_names(templates),
        "last_job_id": coordinator.last_job_id,
        "ha_printed_count": coordinator.ha_printed_count,
    }
