"""Shared constants and canned labelito API payloads for the test suite.

Payload shapes mirror labelito's HealthResponse / PrinterStatusResponse / TemplateInfo exactly, so
the fixtures exercise the same branches the real client parses.
"""

from __future__ import annotations

from typing import Any

from homeassistant.const import CONF_HOST, CONF_PORT

from custom_components.labelito.const import CONF_API_TOKEN

MOCK_HOST = "192.0.2.10"
MOCK_PORT = 8765
MOCK_TOKEN = "test-token"

MOCK_CONFIG: dict[str, Any] = {
    CONF_HOST: MOCK_HOST,
    CONF_PORT: MOCK_PORT,
    CONF_API_TOKEN: MOCK_TOKEN,
}

BASE_URL = f"http://{MOCK_HOST}:{MOCK_PORT}"

MOCK_SERIAL = "SN0123456789"

# api_version 3 and no `uri`: mirrors a current v3 /health, which dropped the printer URI.
MOCK_HEALTH: dict[str, Any] = {
    "status": "ok",
    "version": "0.8.1",
    "api_version": 3,
    "driver": "QL-810W",
    "model": "QL-810W",
    "transport": "network",
    "template_count": 2,
    "default_language": "en",
    "languages": ["en", "es"],
}

# `uri` lives on /printer/status (all API versions); `firmware` was dropped at v2 and is absent here.
MOCK_STATUS: dict[str, Any] = {
    "reachable": True,
    "state": "idle",
    "uri": "tcp://192.0.2.50:9100",
    "serial": MOCK_SERIAL,
    "model": "QL-810W",
    "model_mismatch": False,
    "media_width_mm": 62,
    "media_length_mm": None,
    "media_type": "continuous",
    "label_lifecount": 1234,
    "console_text": "Ready",
}

MOCK_TEMPLATES: list[dict[str, Any]] = [
    {
        "name": "pantry",
        "description": "Pantry label",
        "label": "62",
        "rotate": 0,
        "fields": {"required": ["title"], "optional": ["subtitle"]},
        "media": {"width_mm": 62, "media_type": "continuous", "length_mm": None},
        "uses_seq": False,
    },
    {
        "name": "freezer",
        "description": "Freezer label",
        "label": "62",
        "rotate": 90,
        "fields": {"required": ["title"], "optional": []},
        "media": None,
        "uses_seq": False,
    },
    {
        # A {{seq}} template: labelito requires a `sequence` spec (and copies == 1) to print it.
        "name": "crate",
        "description": "Numbered crate tag",
        "label": "62",
        "rotate": 0,
        "fields": {"required": ["label"], "optional": []},
        "media": {"width_mm": 62, "media_type": "continuous", "length_mm": None},
        "uses_seq": True,
    },
]
