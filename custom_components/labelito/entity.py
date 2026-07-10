# SPDX-License-Identifier: MIT
"""Shared entity base wiring device info from the printer status and health payloads."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import LabelitoCoordinator


class LabelitoEntity(CoordinatorEntity[LabelitoCoordinator]):
    """Base entity: one device per config entry, identified by the entry's unique id."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: LabelitoCoordinator, key: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        assert entry is not None and entry.unique_id is not None
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        status = coordinator.data or {}
        # Identity fields come from the SNMP status channel and are absent on USB/file
        # transports; fall back to the /health MODEL so the device card still names the printer.
        # No sw_version: API v3 exposes no printer-firmware field (``firmware`` was dropped from
        # /printer/status at v2), so there is nothing to populate it from.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            manufacturer=MANUFACTURER,
            name=entry.title,
            model=status.get("model") or coordinator.health.get("model"),
            serial_number=status.get("serial"),
            configuration_url=coordinator.client.base_url,
        )
