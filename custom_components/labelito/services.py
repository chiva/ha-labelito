# SPDX-License-Identifier: MIT
"""Service registration and the shared print/reprint execution helpers.

Services are registered in ``async_setup`` (not ``async_setup_entry``): domain services should
exist for the lifetime of the integration so an automation calling ``labelito.print`` while the
entry is reloading gets a clear ServiceValidationError instead of "service not found", and so
multiple config entries never race to register/unregister the same service name. The handlers
resolve a loaded config entry at call time.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, NoReturn

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
)
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .api import (
    LabelitoApiError,
    LabelitoAuthError,
    LabelitoConnectionError,
)
from .const import (
    ATTR_CONFIG_ENTRY_ID,
    ATTR_COPIES,
    ATTR_CUT,
    ATTR_DITHER,
    ATTR_DRY_RUN,
    ATTR_FIELDS,
    ATTR_HIGH_RES,
    ATTR_IDEMPOTENCY_KEY,
    ATTR_JOB_ID,
    ATTR_LANGUAGE,
    ATTR_RED,
    ATTR_SEQ_COUNT,
    ATTR_SEQ_PADDING,
    ATTR_SEQ_START,
    ATTR_SEQ_STEP,
    ATTR_SEQUENCE,
    ATTR_STATUS,
    ATTR_TEMPLATE,
    ATTR_TEMPLATE_INLINE,
    ATTR_THRESHOLD,
    DOMAIN,
    JOB_STATUS_DRY_RUN,
    JOB_STATUS_PRINTED,
    MAX_COPIES,
    MAX_SEQ_COUNT,
    MAX_SEQ_PADDING,
    MAX_SEQ_START,
    MAX_SEQ_STEP,
    MAX_TEMPLATE_INLINE_CHARS,
    MAX_THRESHOLD,
    MIN_COPIES,
    MIN_SEQ_COUNT,
    MIN_SEQ_PADDING,
    MIN_SEQ_START,
    MIN_SEQ_STEP,
    MIN_THRESHOLD,
    SEQ_KEY_COUNT,
    SEQ_KEY_PADDING,
    SEQ_KEY_START,
    SEQ_KEY_STEP,
    SERVICE_PRINT,
    SERVICE_REPRINT_LAST,
)

if TYPE_CHECKING:
    from .coordinator import LabelitoCoordinator

SERVICE_PRINT_SCHEMA = vol.Schema(
    vol.All(
        {
            # Exactly one template source: Exclusive rejects supplying both, and the
            # has_at_least_one_key wrapper below rejects supplying neither. Both are length-checked
            # (min 1) so an empty string is rejected here rather than reaching the catalog lookup as
            # a confusing "Unknown template ''".
            vol.Exclusive(ATTR_TEMPLATE, "template_source"): vol.All(cv.string, vol.Length(min=1)),
            vol.Exclusive(ATTR_TEMPLATE_INLINE, "template_source"): vol.All(
                cv.string, vol.Length(min=1, max=MAX_TEMPLATE_INLINE_CHARS)
            ),
            vol.Optional(ATTR_FIELDS, default=dict): vol.Schema({cv.string: object}),
            vol.Optional(ATTR_COPIES, default=MIN_COPIES): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_COPIES, max=MAX_COPIES)
            ),
            vol.Optional(ATTR_DRY_RUN, default=False): cv.boolean,
            vol.Optional(ATTR_LANGUAGE): cv.string,
            vol.Optional(ATTR_CUT): cv.boolean,
            vol.Optional(ATTR_RED): cv.boolean,
            vol.Optional(ATTR_DITHER): cv.boolean,
            vol.Optional(ATTR_HIGH_RES): cv.boolean,
            vol.Optional(ATTR_THRESHOLD): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_THRESHOLD, max=MAX_THRESHOLD)
            ),
            vol.Optional(ATTR_IDEMPOTENCY_KEY): cv.string,
            # Auto-numbering ({{seq}}) batch. No defaults: an absent input is omitted from the request
            # so labelito's SequenceSpec applies its own default. seq_count is what marks a request as
            # a sequence batch (see _build_print_request); the others only shape the number.
            vol.Optional(ATTR_SEQ_COUNT): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SEQ_COUNT, max=MAX_SEQ_COUNT)
            ),
            vol.Optional(ATTR_SEQ_START): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SEQ_START, max=MAX_SEQ_START)
            ),
            vol.Optional(ATTR_SEQ_STEP): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SEQ_STEP, max=MAX_SEQ_STEP)
            ),
            vol.Optional(ATTR_SEQ_PADDING): vol.All(
                vol.Coerce(int), vol.Range(min=MIN_SEQ_PADDING, max=MAX_SEQ_PADDING)
            ),
            vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        },
        cv.has_at_least_one_key(ATTR_TEMPLATE, ATTR_TEMPLATE_INLINE),
    )
)

SERVICE_REPRINT_LAST_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


def resolve_coordinator(hass: HomeAssistant, entry_id: str | None) -> LabelitoCoordinator:
    """Return the coordinator of the targeted (or only) loaded labelito config entry."""
    loaded: list[ConfigEntry] = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.state is ConfigEntryState.LOADED
    ]
    if entry_id is not None:
        for entry in loaded:
            if entry.entry_id == entry_id:
                return entry.runtime_data
        raise ServiceValidationError(f"No loaded labelito config entry with id {entry_id!r}")
    if not loaded:
        raise ServiceValidationError("No labelito printer is configured and loaded")
    if len(loaded) > 1:
        raise ServiceValidationError(
            "Multiple labelito printers are configured; pass config_entry_id to pick one"
        )
    return loaded[0].runtime_data


def _speakable_detail(detail: str | dict[str, Any] | list[Any]) -> str:
    """Flatten a labelito error ``detail`` into one speakable sentence.

    Mirrors labelito's real 409/422/503 shapes: a media mismatch carries
    ``media_loaded``/``media_required``, a fault carries ``errors``, a missing-fields 422 carries
    ``missing_required``, and simple errors are plain strings.
    """
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        loaded = detail.get("media_loaded")
        required = detail.get("media_required")
        if loaded and required:
            return f"The loaded roll is {loaded} but the template needs {required}"
        msg = str(detail.get("msg", "")) or "labelito rejected the request"
        errors = detail.get("errors")
        if isinstance(errors, list) and errors:
            return f"{msg}: {'; '.join(str(e) for e in errors)}"
        missing = detail.get("missing_required")
        if isinstance(missing, list) and missing:
            return f"{msg}: {', '.join(str(m) for m in missing)}"
        return msg
    return str(detail)


def _raise_for_api_error(err: LabelitoApiError, template_names: list[str]) -> NoReturn:
    """Map a labelito API error onto the matching Home Assistant exception type."""
    message = _speakable_detail(err.detail)
    if err.status == 404:
        available = ", ".join(template_names) or "none"
        raise ServiceValidationError(f"{message}. Available templates: {available}") from err
    if err.status == 403:
        # 403 is a server-side authorization/configuration refusal — template_inline used while
        # INLINE_TEMPLATES_ENABLED is off, or a token lacking permission for this operation — not
        # invalid caller input. Surface it as a fault carrying labelito's own detail rather than a
        # ServiceValidationError (which would frame a server/auth misconfiguration as bad input).
        raise HomeAssistantError(f"labelito refused the request (403): {message}") from err
    if err.status in (409, 422):
        raise ServiceValidationError(message) from err
    if err.status == 503:
        raise HomeAssistantError(f"Printer unreachable or busy: {message}") from err
    raise HomeAssistantError(f"labelito error {err.status}: {message}") from err


async def async_validate_template(
    coordinator: LabelitoCoordinator, template: str
) -> list[dict[str, Any]]:
    """Validate the template name against the live catalog at call time.

    A miss forces one cache refresh (a template just added on the server must not need a 15 min
    TTL to become printable); a second miss raises naming the valid templates.
    """
    templates = await coordinator.async_get_templates()
    if not any(t["name"] == template for t in templates):
        templates = await coordinator.async_get_templates(force_refresh=True)
        if not any(t["name"] == template for t in templates):
            names = ", ".join(coordinator.template_names(templates)) or "none"
            raise ServiceValidationError(f"Unknown template {template!r}. Valid templates: {names}")
    return templates


def _record_ha_print(
    coordinator: LabelitoCoordinator, result: dict[str, Any], printed_labels: int
) -> None:
    """Bump the HA-issued labels-printed counter by ``printed_labels`` and push it out immediately.

    Every print path (service call, voice intent, reprint button) funnels through
    async_execute_print or async_reprint_last, so this is the single place the fallback
    labels-printed sensor's backing counter is touched. async_update_listeners() notifies the
    sensor right away instead of waiting up to 90 s for the next USB status poll.

    Callers pass the physical label count: ``result[ATTR_COPIES]`` for a plain print, or the
    sequence batch size for an auto-numbering print — labelito reports ``copies: 1`` for a sequence
    batch (the item count lives in the request's sequence spec, not the response), so the caller
    must supply it.

    Counts each job_id at most once: labelito replays an idempotency-key reuse as the prior
    PrintResponse (same job_id, no physical print), so a retried print with a stable key must not
    increment the counter again. Fresh prints and reprints each carry a new job_id and count.
    """
    if result[ATTR_DRY_RUN]:
        return
    job_id = result[ATTR_JOB_ID]
    if job_id in coordinator.counted_job_ids:
        return
    coordinator.counted_job_ids.add(job_id)
    coordinator.ha_printed_count += printed_labels
    coordinator.async_update_listeners()


async def async_execute_print(
    coordinator: LabelitoCoordinator, request: dict[str, Any]
) -> dict[str, Any]:
    """Send a validated PrintRequest, mapping errors and recording the job for reprint-last.

    idempotency_key (a PrintRequest body field, per labelito's models) lets the server dedupe a
    replayed request. When a caller supplies one via the service (``_build_print_request`` passes
    it through), retrying the *same* logical print after an ambiguous failure — a lost response or
    timeout after the POST reached labelito — reuses that key so the physical label prints once.
    Absent one, a fresh key is generated per call: two distinct calls with identical content still
    print twice as intended, but an automation that wants retry-safety must pass a stable key.
    """
    request.setdefault("idempotency_key", str(uuid.uuid4()))
    try:
        result = await coordinator.client.print_label(request)
    except LabelitoAuthError as err:
        raise HomeAssistantError("labelito rejected the API token") from err
    except LabelitoConnectionError as err:
        raise HomeAssistantError(f"Printer unreachable: {err}") from err
    except LabelitoApiError as err:
        # Only a 404 needs the template catalog (to name the valid templates); fetch it lazily so a
        # normal print — and every inline print, which never names a stored template — doesn't pay a
        # catalog round trip on the hot path.
        names = (
            coordinator.template_names(await coordinator.async_get_templates())
            if err.status == 404
            else []
        )
        _raise_for_api_error(err, names)
    coordinator.last_job_id = result[ATTR_JOB_ID]
    # A sequence batch prints sequence.count labels but labelito echoes copies=1, so the batch size
    # is read from the request we just sent, not the response. Remember it for reprint-last.
    printed_labels = (
        request[ATTR_SEQUENCE][SEQ_KEY_COUNT] if ATTR_SEQUENCE in request else result[ATTR_COPIES]
    )
    coordinator.last_job_labels = printed_labels
    _record_ha_print(coordinator, result, printed_labels)
    return result


async def async_reprint_last(coordinator: LabelitoCoordinator) -> dict[str, Any]:
    """Replay the last job printed through this integration via POST /reprint/{job_id}."""
    job_id = coordinator.last_job_id
    if job_id is None:
        raise ServiceValidationError(
            "Nothing to reprint: no label has been printed through Home Assistant "
            "since the last restart"
        )
    try:
        result = await coordinator.client.reprint(job_id, coordinator.last_job_labels)
    except LabelitoAuthError as err:
        raise HomeAssistantError("labelito rejected the API token") from err
    except LabelitoConnectionError as err:
        raise HomeAssistantError(f"Printer unreachable: {err}") from err
    except LabelitoApiError as err:
        if err.status == 404:
            # The job fell out of labelito's retained history; drop the stale reference and
            # skip _raise_for_api_error — its 404 branch appends an "available templates" hint
            # that makes no sense for a job-id miss.
            coordinator.last_job_id = None
            raise ServiceValidationError(
                f"{_speakable_detail(err.detail)}. Print a new label first."
            ) from err
        _raise_for_api_error(err, [])
    # A replayed sequence batch reprints all its labels, and PrintResponse carries no item count, so
    # credit the count recorded when the original job printed (last_job_labels, set in lockstep with
    # last_job_id) rather than the echoed copies=1.
    _record_ha_print(coordinator, result, coordinator.last_job_labels)
    return result


def _build_print_request(data: dict[str, Any]) -> dict[str, Any]:
    """Translate validated service-call data into a labelito PrintRequest body.

    Optional knobs are only included when explicitly provided: PrintRequest uses
    nullable-inherit semantics (an absent/None option inherits the server default), so sending
    a hardcoded value would override server configuration.
    """
    request: dict[str, Any] = {
        ATTR_FIELDS: data[ATTR_FIELDS],
        ATTR_COPIES: data[ATTR_COPIES],
        ATTR_DRY_RUN: data[ATTR_DRY_RUN],
    }
    # Exactly one template source is present (schema Exclusive + _validate_template_source).
    if ATTR_TEMPLATE in data:
        request[ATTR_TEMPLATE] = data[ATTR_TEMPLATE]
    else:
        request[ATTR_TEMPLATE_INLINE] = data[ATTR_TEMPLATE_INLINE]
    if ATTR_LANGUAGE in data:
        request[ATTR_LANGUAGE] = data[ATTR_LANGUAGE]
    if ATTR_CUT in data:
        request[ATTR_CUT] = data[ATTR_CUT]
    if ATTR_IDEMPOTENCY_KEY in data:
        request[ATTR_IDEMPOTENCY_KEY] = data[ATTR_IDEMPOTENCY_KEY]
    options = {
        key: data[key]
        for key in (ATTR_RED, ATTR_DITHER, ATTR_HIGH_RES, ATTR_THRESHOLD)
        if key in data
    }
    if options:
        request["options"] = options
    # seq_count marks the request as an auto-numbering batch; the other seq_* inputs only shape the
    # number and are omitted when unset so labelito's SequenceSpec applies its defaults. Cross-field
    # rules (count-required, copies exclusivity) are enforced in _validate_sequence before this runs.
    if ATTR_SEQ_COUNT in data:
        sequence: dict[str, Any] = {SEQ_KEY_COUNT: data[ATTR_SEQ_COUNT]}
        if ATTR_SEQ_START in data:
            sequence[SEQ_KEY_START] = data[ATTR_SEQ_START]
        if ATTR_SEQ_STEP in data:
            sequence[SEQ_KEY_STEP] = data[ATTR_SEQ_STEP]
        if ATTR_SEQ_PADDING in data:
            sequence[SEQ_KEY_PADDING] = data[ATTR_SEQ_PADDING]
        request[ATTR_SEQUENCE] = sequence
    return request


def _validate_sequence(data: dict[str, Any]) -> None:
    """Reject invalid auto-numbering input before it reaches the printer service.

    Two cross-field rules that labelito would otherwise return as a 422, surfaced here as clear,
    fail-fast errors (mirroring async_validate_template's client-side template check):

    * A seq_start/seq_step/seq_padding without seq_count — the batch size is undefined; seq_count is
      the required anchor of a SequenceSpec.
    * seq_count together with copies > 1 — labelito's ``sequence`` and ``copies`` are mutually
      exclusive: sequence drives the item count, copies multiplies identical labels.

    The ``{{seq}}``-template biconditional (a sequence is required iff the template uses ``{{seq}}``)
    is left to labelito, whose 422 is already rendered speakably by _speakable_detail.
    """
    other_seq_inputs = {ATTR_SEQ_START, ATTR_SEQ_STEP, ATTR_SEQ_PADDING} & data.keys()
    if ATTR_SEQ_COUNT not in data:
        if other_seq_inputs:
            raise ServiceValidationError(
                "Auto-numbering needs 'seq_count' (the number of labels in the batch) when any of "
                "seq_start / seq_step / seq_padding is set."
            )
        return
    if data[ATTR_COPIES] > MIN_COPIES:
        raise ServiceValidationError(
            "'seq_count' and 'copies' > 1 are mutually exclusive: a sequence already prints "
            "seq_count labels. Leave copies at 1 (the default) when auto-numbering."
        )


def async_setup_services(hass: HomeAssistant) -> None:
    """Register the labelito.print and labelito.reprint_last services."""

    async def _handle_print(call: ServiceCall) -> ServiceResponse:
        # Exactly-one-template-source and field bounds are enforced by SERVICE_PRINT_SCHEMA before
        # the handler runs. The remaining cross-field sequence rules are checked before resolving
        # the printer, so a bad sequence surfaces its own error rather than being masked by a
        # "no/multiple printers configured" error from resolve_coordinator.
        data = dict(call.data)
        _validate_sequence(data)
        coordinator = resolve_coordinator(hass, data.get(ATTR_CONFIG_ENTRY_ID))
        # An inline body has no catalog entry to validate against; only named templates are
        # checked against the live template list.
        if ATTR_TEMPLATE in data:
            await async_validate_template(coordinator, data[ATTR_TEMPLATE])
        result = await async_execute_print(coordinator, _build_print_request(data))
        if not call.return_response:
            return None
        return {
            ATTR_JOB_ID: result[ATTR_JOB_ID],
            ATTR_STATUS: JOB_STATUS_DRY_RUN if result[ATTR_DRY_RUN] else JOB_STATUS_PRINTED,
        }

    async def _handle_reprint_last(call: ServiceCall) -> ServiceResponse:
        coordinator = resolve_coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        result = await async_reprint_last(coordinator)
        if not call.return_response:
            return None
        return {
            ATTR_JOB_ID: result[ATTR_JOB_ID],
            ATTR_STATUS: JOB_STATUS_DRY_RUN if result[ATTR_DRY_RUN] else JOB_STATUS_PRINTED,
        }

    hass.services.async_register(
        DOMAIN,
        SERVICE_PRINT,
        _handle_print,
        schema=SERVICE_PRINT_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_REPRINT_LAST,
        _handle_reprint_last,
        schema=SERVICE_REPRINT_LAST_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
