# SPDX-License-Identifier: MIT
"""The LabelitoPrint intent: voice-driven label printing via Assist.

Requires the user to copy the shipped ``custom_sentences/`` files into
``<config>/custom_sentences/<lang>/`` — integrations cannot bundle custom sentences.
"""

from __future__ import annotations

import difflib
from typing import Any, ClassVar

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent

from .api import LabelitoApiError
from .const import (
    ATTR_COPIES,
    ATTR_DRY_RUN,
    ATTR_FIELDS,
    ATTR_LANGUAGE,
    ATTR_TEMPLATE,
    INTENT_PRINT,
)
from .coordinator import LabelitoCoordinator
from .services import async_execute_print, resolve_coordinator

# labelito's HTTP status for a request that omits a template's required fields (matches
# services._raise_for_api_error). The intent handler translates it into the spoken needs_text reply.
HTTP_UNPROCESSABLE_CONTENT = 422

SLOT_TEMPLATE = "template"
SLOT_TEXT = "text"

DEFAULT_SPEECH_LANGUAGE = "en"

# Spoken confirmations/errors, keyed by the primary language subtag of the Assist request.
# The resolved language also rides on the print request itself so any printed chrome
# (labelito's [[translation]] tokens, {{date}} formatting) matches the spoken language.
SPEECH: dict[str, dict[str, str]] = {
    "en": {
        "printed": "Printed a {template} label.",
        "printed_text": "Printed a {template} label for {text}.",
        "unknown_template": (
            "I don't know a label template called {template}. Available templates are: {templates}."
        ),
        "no_templates": "The label printer has no templates configured.",
        "needs_text": "I need the text to put on the {template} label.",
        "failed": "I couldn't print the label: {reason}",
    },
    "es": {
        "printed": "He imprimido una etiqueta de {template}.",
        "printed_text": "He imprimido una etiqueta de {template} para {text}.",
        "unknown_template": (
            "No conozco ninguna plantilla de etiqueta llamada {template}. "
            "Las plantillas disponibles son: {templates}."
        ),
        "no_templates": "La impresora de etiquetas no tiene plantillas configuradas.",
        "needs_text": "Necesito el texto para la etiqueta de {template}.",
        "failed": "No he podido imprimir la etiqueta: {reason}",
    },
}

FUZZY_MATCH_CUTOFF = 0.6

# Connector phrases that sit between {template} and {text} in the sentence files (es: "para",
# "que diga"; en: "for", "that says"). When HA's recognize_best collapses the whole utterance into
# the greedy trailing {template} wildcard (see docs/voice-assist.md), exactly one of these leading
# phrases is stripped to recover the free text. Ordered longest-first so multi-word phrases win.
# See _split_template_and_text.
CONNECTOR_PHRASES: tuple[tuple[str, ...], ...] = (
    ("que", "diga"),
    ("that", "says"),
    ("para",),
    ("for",),
)


def _speech_language(language: str | None) -> str:
    primary = (language or "").split("-")[0].lower()
    return primary if primary in SPEECH else DEFAULT_SPEECH_LANGUAGE


def _normalize(name: str) -> str:
    return name.lower().replace("-", " ").replace("_", " ").strip()


def _fuzzy_match_template(spoken: str, templates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a spoken template name against the catalog: exact, then substring, then fuzzy close.

    Substring containment is checked before the generic ``get_close_matches`` pass: full containment
    (the spoken value is a substring of a template name, or vice versa) is a stronger signal than a
    fuzzy ratio, so "gift" resolves to a ``gift-box`` template rather than a coincidental typo
    neighbour like ``grift``. The longest overlapping name wins, so an overlapping catalog (e.g.
    freezer / freezer-dated) resolves to the more specific template regardless of catalog order.
    """
    by_normalized = {_normalize(t["name"]): t for t in templates}
    wanted = _normalize(spoken)
    if wanted in by_normalized:
        return by_normalized[wanted]
    substring_matches = [
        (len(normalized), template)
        for normalized, template in by_normalized.items()
        if wanted in normalized or normalized in wanted
    ]
    if substring_matches:
        return max(substring_matches, key=lambda item: item[0])[1]
    close = difflib.get_close_matches(wanted, list(by_normalized), n=1, cutoff=FUZZY_MATCH_CUTOFF)
    if close:
        return by_normalized[close[0]]
    return None


def _split_template_and_text(
    spoken: str, templates: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the template and recover any free text folded into the ``template`` wildcard.

    HA's ``recognize_best`` collapses "<template> <connector> <text>" into the single greedy
    trailing ``{template}`` wildcard for languages whose sentences lack a literal after it (see
    docs/voice-assist.md).

    An exactly-spoken template name always wins first — even one that contains connector words — so
    "gift for christmas" resolves to a ``gift-for-christmas`` template rather than ``gift`` + text.

    Otherwise, **template names are assumed to contain no connector words** (``para``/``for``/``que
    diga``/``that says``): the *first* connector phrase is then the template/text boundary —
    everything before it is the template name (matched exactly or fuzzily, so ASR variants like
    "pantri" still resolve), everything after is the spoken text (which may itself contain
    connectors — only the first is consumed). With no connector, the whole utterance is a template
    name and there is no free text.
    """
    by_normalized = {_normalize(t["name"]): t for t in templates}
    if _normalize(spoken) in by_normalized:
        return by_normalized[_normalize(spoken)], None

    tokens = spoken.split()
    normalized = [_normalize(token) for token in tokens]
    for index in range(1, len(tokens)):
        phrase = next(
            (p for p in CONNECTOR_PHRASES if tuple(normalized[index : index + len(p)]) == p),
            None,
        )
        if phrase is None:
            continue
        template = _fuzzy_match_template(" ".join(tokens[:index]), templates)
        if template is not None:
            return template, " ".join(tokens[index + len(phrase) :]).strip() or None
        break  # first connector's prefix did not resolve — fall back to a whole-utterance match

    return _fuzzy_match_template(spoken, templates), None


def _text_field_name(template: dict[str, Any]) -> str | None:
    """The field a free-form spoken text should fill: the first required field, else the first
    optional one (mirrors TemplateFieldContract's required/optional split)."""
    fields = template.get(ATTR_FIELDS) or {}
    for bucket in ("required", "optional"):
        names = fields.get(bucket) or []
        if names:
            return str(names[0])
    return None


def _is_missing_required_error(err: Exception) -> bool:
    """True when ``err`` came from labelito rejecting a print for missing required fields.

    ``services._raise_for_api_error`` raises ``ServiceValidationError(...) from LabelitoApiError``
    for a 422, so the structured ``missing_required`` detail is still reachable via ``__cause__``.
    """
    cause = err.__cause__
    return (
        isinstance(cause, LabelitoApiError)
        and cause.status == HTTP_UNPROCESSABLE_CONTENT
        and isinstance(cause.detail, dict)
        and bool(cause.detail.get("missing_required"))
    )


class LabelitoPrintIntentHandler(intent.IntentHandler):
    """Handle "print a <template> label for <text>" requests from Assist."""

    intent_type = INTENT_PRINT
    description = (
        "Prints a physical label on the Brother QL label printer. Requires the name of a "
        "labelito template (for example 'pantry' or 'freezer-dated'); optionally takes the "
        "free-form text to put on the label. Use only when the user asks to print a label."
    )
    slot_schema: ClassVar = {
        vol.Required(SLOT_TEMPLATE): cv.string,
        vol.Optional(SLOT_TEXT): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        hass: HomeAssistant = intent_obj.hass
        response = intent_obj.create_response()
        language = _speech_language(intent_obj.language)
        speech = SPEECH[language]

        slots = self.async_validate_slots(intent_obj.slots)
        spoken_template: str = slots[SLOT_TEMPLATE]["value"]
        text: str | None = slots.get(SLOT_TEXT, {}).get("value")

        try:
            coordinator = resolve_coordinator(hass, None)
            template, recovered_text = await self._async_match_template(
                coordinator, spoken_template
            )
        except (HomeAssistantError, ServiceValidationError) as err:
            return self._error(response, speech["failed"].format(reason=err))
        if template is None:
            templates = await coordinator.async_get_templates()
            names = coordinator.template_names(templates)
            if not names:
                return self._error(response, speech["no_templates"])
            return self._error(
                response,
                speech["unknown_template"].format(
                    template=spoken_template, templates=", ".join(names)
                ),
            )

        # recognize_best may have folded the free text into the template wildcard; fall back to the
        # text recovered while resolving the template, but never clobber an explicit text slot.
        text = text or recovered_text

        request: dict[str, Any] = {
            ATTR_TEMPLATE: template["name"],
            ATTR_FIELDS: {},
            ATTR_COPIES: 1,
            ATTR_DRY_RUN: False,
            ATTR_LANGUAGE: language,
        }
        if text:
            field_name = _text_field_name(template)
            if field_name is not None:
                request[ATTR_FIELDS] = {field_name: text}

        try:
            await async_execute_print(coordinator, request)
        except (HomeAssistantError, ServiceValidationError) as err:
            # labelito is authoritative on required fields — do not veto on cached template metadata
            # (which can be stale for up to the catalog TTL). Reframe its "missing required fields"
            # 422 as the needs_text prompt only when the user gave no text at all; if text *was*
            # supplied but a (second or renamed) field is still missing, surface the server's field
            # names verbatim rather than misleadingly asking for text again.
            if not text and _is_missing_required_error(err):
                return self._error(response, speech["needs_text"].format(template=template["name"]))
            return self._error(response, speech["failed"].format(reason=err))

        key = "printed_text" if text else "printed"
        response.async_set_speech(speech[key].format(template=template["name"], text=text))
        return response

    @staticmethod
    async def _async_match_template(
        coordinator: LabelitoCoordinator, spoken: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        templates = await coordinator.async_get_templates()
        match, recovered_text = _split_template_and_text(spoken, templates)
        if match is None:
            # Same freshness rule as the service path: a miss forces one catalog refresh.
            templates = await coordinator.async_get_templates(force_refresh=True)
            match, recovered_text = _split_template_and_text(spoken, templates)
        return match, recovered_text

    @staticmethod
    def _error(response: intent.IntentResponse, message: str) -> intent.IntentResponse:
        response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, message)
        return response


def async_setup_intents(hass: HomeAssistant) -> None:
    intent.async_register(hass, LabelitoPrintIntentHandler())
