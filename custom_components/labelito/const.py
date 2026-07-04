# SPDX-License-Identifier: MIT
"""Constants for the labelito integration."""

from datetime import timedelta
from typing import Final

DOMAIN: Final = "labelito"
MANUFACTURER: Final = "Brother"

# HTTP API compatibility gate, checked against HealthResponse.api_version at config time.
# labelito bumps api_version only on breaking changes; additive changes keep the number.
MIN_API_VERSION: Final = 1
MAX_API_VERSION: Final = 1

DEFAULT_PORT: Final = 8765

CONF_API_TOKEN: Final = "api_token"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# /health reports the transport inferred from PRINTER_URI: "network", "usb", or "file".
TRANSPORT_NETWORK: Final = "network"
TRANSPORT_USB: Final = "usb"
TRANSPORT_FILE: Final = "file"
TRANSPORTS: Final = [TRANSPORT_NETWORK, TRANSPORT_USB, TRANSPORT_FILE]

# The network transport answers status over SNMP without touching the print path, so it can be
# polled tightly. A USB status read claims the single device handle and serializes behind the
# print lock server-side, so it is polled far less aggressively.
SCAN_INTERVAL_NETWORK: Final = timedelta(seconds=30)
SCAN_INTERVAL_USB: Final = timedelta(seconds=90)

TEMPLATE_CACHE_TTL: Final = timedelta(minutes=15)

# PrinterStatusResponse.state vocabulary (labelito's PrinterState enum).
PRINTER_STATE_OFF: Final = "off"
PRINTER_STATE_IDLE: Final = "idle"
PRINTER_STATE_PRINTING: Final = "printing"
PRINTER_STATE_ERROR: Final = "error"
PRINTER_STATES: Final = [
    PRINTER_STATE_OFF,
    PRINTER_STATE_IDLE,
    PRINTER_STATE_PRINTING,
    PRINTER_STATE_ERROR,
]

# PrintJobRecord.status values surfaced in the labelito.print service response.
JOB_STATUS_PRINTED: Final = "printed"
JOB_STATUS_DRY_RUN: Final = "dry-run"

SERVICE_PRINT: Final = "print"
SERVICE_REPRINT_LAST: Final = "reprint_last"

INTENT_PRINT: Final = "LabelitoPrint"

ATTR_TEMPLATE: Final = "template"
ATTR_FIELDS: Final = "fields"
ATTR_COPIES: Final = "copies"
ATTR_DRY_RUN: Final = "dry_run"
ATTR_LANGUAGE: Final = "language"
ATTR_CUT: Final = "cut"
ATTR_RED: Final = "red"
ATTR_DITHER: Final = "dither"
ATTR_IDEMPOTENCY_KEY: Final = "idempotency_key"
ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"

ATTR_JOB_ID: Final = "job_id"
ATTR_STATUS: Final = "status"

# PrintRequest.copies bounds (Field(ge=1, le=10) in labelito's models).
MIN_COPIES: Final = 1
MAX_COPIES: Final = 10
