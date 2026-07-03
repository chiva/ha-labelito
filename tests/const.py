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

MOCK_HEALTH: dict[str, Any] = {
    "status": "ok",
    "version": "0.1.3",
    "api_version": 1,
    "driver": "QL-810W",
    "model": "QL-810W",
    "transport": "network",
    "uri": "tcp://192.0.2.50:9100",
    "template_count": 2,
    "default_language": "en",
    "languages": ["en", "es"],
}

MOCK_STATUS: dict[str, Any] = {
    "reachable": True,
    "state": "idle",
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
    },
    {
        "name": "freezer",
        "description": "Freezer label",
        "label": "62",
        "rotate": 90,
        "fields": {"required": ["title"], "optional": []},
        "media": None,
    },
]
