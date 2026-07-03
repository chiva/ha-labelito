# SPDX-License-Identifier: MIT
"""Binary sensors: printer connectivity and problem state."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LabelitoConfigEntry
from .const import PRINTER_STATE_ERROR
from .coordinator import LabelitoCoordinator
from .entity import LabelitoEntity

KEY_CONNECTIVITY = "connectivity"
KEY_PROBLEM = "problem"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LabelitoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    async_add_entities(
        [
            LabelitoConnectivitySensor(coordinator),
            LabelitoProblemSensor(coordinator),
        ]
    )


class LabelitoConnectivitySensor(LabelitoEntity, BinarySensorEntity):
    """Whether the physical printer answered the last status query (``reachable``)."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = KEY_CONNECTIVITY

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_CONNECTIVITY)

    @property
    def is_on(self) -> bool:
        return bool(self.coordinator.data.get("reachable"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"uri": self.coordinator.data.get("uri")}


class LabelitoProblemSensor(LabelitoEntity, BinarySensorEntity):
    """On when the printer reports a fault or the configured MODEL mismatches the device."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = KEY_PROBLEM

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_PROBLEM)

    @property
    def is_on(self) -> bool:
        data = self.coordinator.data
        return (
            data.get("state") == PRINTER_STATE_ERROR
            or bool(data.get("errors"))
            or bool(data.get("model_mismatch"))
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "errors": data.get("errors", []),
            "console_text": data.get("console_text"),
            "model_mismatch": data.get("model_mismatch", False),
        }
