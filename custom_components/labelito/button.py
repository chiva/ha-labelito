# SPDX-License-Identifier: MIT
"""Button: reprint the last label printed through Home Assistant."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LabelitoConfigEntry
from .coordinator import LabelitoCoordinator
from .entity import LabelitoEntity
from .services import async_reprint_last

KEY_REPRINT_LAST = "reprint_last"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LabelitoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([LabelitoReprintLastButton(entry.runtime_data)])


class LabelitoReprintLastButton(LabelitoEntity, ButtonEntity):
    """Mirrors the labelito.reprint_last service (POST /reprint/{job_id} on the last job)."""

    _attr_translation_key = KEY_REPRINT_LAST

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_REPRINT_LAST)

    async def async_press(self) -> None:
        await async_reprint_last(self.coordinator)
