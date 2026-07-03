"""Tests for the sensor platform's mapping of the labelito status payload.

These lock the entity field mapping to the real PrinterStatusResponse shape (nested ``media``,
``labels_printed``, ``display``) so a drift between the payload and the sensor code — the kind that
would silently render media Unknown or drop the display sensor — fails the suite instead.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from .conftest import register_labelito
from .const import MOCK_HEALTH, MOCK_SERIAL


def _sensor_state(hass: HomeAssistant, registry: er.EntityRegistry, key: str):
    """Resolve a Labelito sensor by its unique-id key and return its state."""
    entity_id = registry.async_get_entity_id("sensor", "labelito", f"{MOCK_SERIAL}_{key}")
    assert entity_id is not None, f"no sensor entity for key {key!r}"
    return hass.states.get(entity_id)


async def test_network_status_maps_to_sensors(
    hass: HomeAssistant, mock_labelito: AiohttpClientMocker, mock_config_entry: MockConfigEntry
) -> None:
    """A reachable network printer surfaces nested media, the SNMP odometer, and the display."""
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)

    media = _sensor_state(hass, registry, "media")
    assert media.state == "62mm continuous"
    assert media.attributes["width_mm"] == 62
    assert media.attributes["media_type"] == "continuous"

    labels = _sensor_state(hass, registry, "labels_printed")
    assert labels.state == "1234"
    assert labels.attributes["source"] == "printer"

    display = _sensor_state(hass, registry, "console_text")
    assert display.state == "Ready"


async def test_usb_status_uses_fallback_counter_and_omits_display(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """USB transport has no SNMP odometer/display: fall back to the HA counter, drop the display."""
    usb_status = {
        "reachable": True,
        "state": "idle",
        "serial": MOCK_SERIAL,
        "model": "QL-810W",
        "model_mismatch": False,
        "media_width_mm": 62,
        "media_length_mm": None,
        "media_type": "continuous",
    }
    register_labelito(aioclient_mock, health={**MOCK_HEALTH, "transport": "usb"}, status=usb_status)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)

    media = _sensor_state(hass, registry, "media")
    assert media.state == "62mm continuous"

    labels = _sensor_state(hass, registry, "labels_printed")
    assert labels.attributes["source"] == "home_assistant"

    display_id = registry.async_get_entity_id("sensor", "labelito", f"{MOCK_SERIAL}_console_text")
    assert display_id is None


async def test_network_without_snmp_telemetry_uses_fallback_counter(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    mock_config_entry: MockConfigEntry,
) -> None:
    """A network status carrying no SNMP telemetry uses the HA fallback counter and no display.

    This is the shape labelito returns for BOTH an SNMP-disabled network printer (SNMP_ENABLED=false
    falls to the ESC i S path) and a network printer that is simply powered off at setup: a 503 body
    with reachable=false and neither label_lifecount nor console_text. The two are indistinguishable
    over the API, and /health exposes no SNMP capability flag, so the sensors gate on field presence:
    absent telemetry means the HA-issued counter and no display sensor. (An SNMP-capable printer that
    was merely off recovers its lifetime odometer on the next reload once it reports the field.)
    """
    no_telemetry_status = {
        "reachable": False,
        "state": "off",
        "serial": MOCK_SERIAL,
        "model": "QL-810W",
        "model_mismatch": False,
    }
    register_labelito(aioclient_mock, status=no_telemetry_status, status_code=503)
    mock_config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()

    registry = er.async_get(hass)

    labels = _sensor_state(hass, registry, "labels_printed")
    assert labels.attributes["source"] == "home_assistant"

    display_id = registry.async_get_entity_id("sensor", "labelito", f"{MOCK_SERIAL}_console_text")
    assert display_id is None
