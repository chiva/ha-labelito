# SPDX-License-Identifier: MIT
"""Sensors: printer state, loaded media, labels printed (SNMP odometer or a persistent
Home-Assistant-issued fallback counter), console text, labelito version, template count,
transport, and printer model.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    RestoreSensor,
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import LabelitoConfigEntry
from .const import PRINTER_STATES, TRANSPORTS
from .coordinator import LabelitoCoordinator
from .entity import LabelitoEntity

KEY_STATE = "printer_state"
KEY_MEDIA = "media"
KEY_LABELS_PRINTED = "labels_printed"
KEY_CONSOLE_TEXT = "console_text"
KEY_VERSION = "version"
KEY_TEMPLATE_COUNT = "template_count"
KEY_TRANSPORT = "transport"
KEY_PRINTER_MODEL = "printer_model"

# Values for the two labels_printed backends' "source" attribute.
SOURCE_PRINTER = "printer"
SOURCE_HOME_ASSISTANT = "home_assistant"


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LabelitoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = entry.runtime_data
    entities: list[SensorEntity] = [
        LabelitoStateSensor(coordinator),
        LabelitoMediaSensor(coordinator),
        LabelitoVersionSensor(coordinator),
        LabelitoTemplateCountSensor(coordinator),
        LabelitoTransportSensor(coordinator),
        LabelitoPrinterModelSensor(coordinator),
    ]
    # label_lifecount (SNMP marker-life odometer) and console_text are populated only on a
    # network+SNMP deployment; gate on their presence in the first status payload and fall back to
    # the HA-issued counter (and no display sensor) otherwise.
    #
    # This deliberately gates on the payload, not the /health transport, because labelito cannot
    # tell the client which network deployments actually have SNMP: /health reports transport purely
    # from the URI scheme, and a network printer with SNMP_ENABLED=false returns the SAME
    # reachable=false/no-fields status as a network printer that is merely powered off. Since those
    # two are indistinguishable and there is no capability flag in the contract, presence-gating is
    # the least-bad choice: an SNMP-disabled site correctly gets a working HA counter, and the only
    # downgrade is that a network printer powered off at setup also gets the (still functional) HA
    # counter until the next reload rather than its lifetime odometer. Evaluated once at setup.
    if coordinator.data.get("label_lifecount") is not None:
        entities.append(LabelitoLabelsPrintedSensor(coordinator))
    else:
        entities.append(LabelitoLabelsPrintedViaHaSensor(coordinator))
    if coordinator.data.get("console_text") is not None:
        entities.append(LabelitoConsoleTextSensor(coordinator))
    async_add_entities(entities)


class LabelitoStateSensor(LabelitoEntity, SensorEntity):
    """Mirrors labelito's derived PrinterState: off / idle / printing / error."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = PRINTER_STATES
    _attr_translation_key = KEY_STATE

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_STATE)

    @property
    def native_value(self) -> str | None:
        state = self.coordinator.data.get("state")
        return state if state in PRINTER_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {"status": data.get("status"), "phase": data.get("phase")}


class LabelitoMediaSensor(LabelitoEntity, SensorEntity):
    """The loaded roll, described like labelito's own 409 details (e.g. '62mm continuous')."""

    _attr_translation_key = KEY_MEDIA

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_MEDIA)

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        width: float | None = data.get("media_width_mm")
        media_type: str | None = data.get("media_type")
        if width is None or media_type is None:
            return None
        kind = "die-cut" if media_type == "die_cut" else media_type
        length: float | None = data.get("media_length_mm")
        if media_type == "die_cut" and length:
            return f"{width:g}x{length:g}mm {kind}"
        return f"{width:g}mm {kind}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        data = self.coordinator.data
        return {
            "width_mm": data.get("media_width_mm"),
            "length_mm": data.get("media_length_mm"),
            "media_type": data.get("media_type"),
        }


class LabelitoLabelsPrintedSensor(LabelitoEntity, SensorEntity):
    """Lifetime label count from the printer's SNMP marker-life counter (network transport)."""

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = KEY_LABELS_PRINTED
    _attr_native_unit_of_measurement = "labels"

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_LABELS_PRINTED)

    @property
    def native_value(self) -> int | None:
        count = self.coordinator.data.get("label_lifecount")
        return count if isinstance(count, int) else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"source": SOURCE_PRINTER}


class LabelitoLabelsPrintedViaHaSensor(LabelitoEntity, RestoreSensor):
    """Fallback labels-printed counter for USB/file deployments with no SNMP odometer.

    Counts only labels printed *through this Home Assistant instance* (service call, voice
    intent, or the reprint button) — it cannot see labels printed any other way, unlike the SNMP
    odometer's true lifetime count. A transport change (network <-> USB/file) after setup swaps
    which of the two labels_printed backends owns this entity id, since the choice is made once
    at platform setup.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_translation_key = KEY_LABELS_PRINTED
    _attr_native_unit_of_measurement = "labels"

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_LABELS_PRINTED)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        restored = await self.async_get_last_sensor_data()
        restored_count = restored.native_value if restored is not None else None
        if not isinstance(restored_count, (int, float)):
            restored_count = 0
        # max(): a restart must never lose prints already counted (restored beats a fresh
        # coordinator's 0), and a same-run platform reload must never regress a coordinator that
        # already counted prints this run (in-memory beats a stale restored value).
        self.coordinator.ha_printed_count = max(
            int(restored_count), self.coordinator.ha_printed_count
        )

    @property
    def native_value(self) -> int:
        return self.coordinator.ha_printed_count

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        return {"source": SOURCE_HOME_ASSISTANT}


class LabelitoConsoleTextSensor(LabelitoEntity, SensorEntity):
    """Raw console/display text line from the printer's SNMP status reply."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = KEY_CONSOLE_TEXT
    _attr_icon = "mdi:message-text-outline"

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_CONSOLE_TEXT)

    @property
    def native_value(self) -> str | None:
        return self.coordinator.data.get("console_text")


class LabelitoVersionSensor(LabelitoEntity, SensorEntity):
    """The labelito service's own version, from /health (fetched once at setup)."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = KEY_VERSION
    _attr_icon = "mdi:package-variant"

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_VERSION)

    @property
    def native_value(self) -> str | None:
        return self.coordinator.health.get("version")


class LabelitoTemplateCountSensor(LabelitoEntity, SensorEntity):
    """Number of print templates labelito currently serves."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_translation_key = KEY_TEMPLATE_COUNT
    _attr_native_unit_of_measurement = "templates"
    _attr_icon = "mdi:file-document-multiple-outline"

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_TEMPLATE_COUNT)

    @property
    def native_value(self) -> int | None:
        return self.coordinator.cached_template_count


class LabelitoTransportSensor(LabelitoEntity, SensorEntity):
    """How labelito talks to the printer: network (SNMP), usb, or file."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = TRANSPORTS
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = KEY_TRANSPORT
    _attr_icon = "mdi:connection"

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_TRANSPORT)

    @property
    def native_value(self) -> str | None:
        transport = self.coordinator.health.get("transport")
        return transport if transport in TRANSPORTS else None


class LabelitoPrinterModelSensor(LabelitoEntity, SensorEntity):
    """The printer model: SNMP-reported identity when reachable, else the configured MODEL.

    Duplicates the device-info Model field by design: device info isn't addressable from
    automations or dashboards, so this sensor exists for that surface even though the value is
    already visible on the device card.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = KEY_PRINTER_MODEL
    _attr_icon = "mdi:printer"

    def __init__(self, coordinator: LabelitoCoordinator) -> None:
        super().__init__(coordinator, KEY_PRINTER_MODEL)

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data
        return data.get("model") or self.coordinator.health.get("model")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"model_mismatch": self.coordinator.data.get("model_mismatch", False)}
