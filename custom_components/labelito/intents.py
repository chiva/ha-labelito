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

# Stricter cutoff for accepting the *whole* utterance as one template name before attempting a
# connector split. High on purpose: it must fire for an ASR variant of a connector-containing name
# ("freezer for leftover" → "freezer-for-leftovers") without swallowing a real "<template>
# <connector> <text>" command, where the added text pushes the whole-string ratio well below this.
WHOLE_TEMPLATE_MATCH_CUTOFF = 0.8

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
    """Match a spoken template name against the catalog: exact, then close, then substring."""
    by_normalized = {_normalize(t["name"]): t for t in templates}
    wanted = _normalize(spoken)
    if wanted in by_normalized:
        return by_normalized[wanted]
    close = difflib.get_close_matches(wanted, list(by_normalized), n=1, cutoff=FUZZY_MATCH_CUTOFF)
    if close:
        return by_normalized[close[0]]
    # Prefer the longest overlapping name so an overlapping catalog (e.g. freezer / freezer-dated)
    # resolves to the more specific template regardless of catalog order.
    substring_matches = [
        (len(normalized), template)
        for normalized, template in by_normalized.items()
        if wanted in normalized or normalized in wanted
    ]
    if substring_matches:
        return max(substring_matches, key=lambda item: item[0])[1]
    return None


def _strip_leading_connector(tokens: list[str]) -> list[str] | None:
    """If ``tokens`` begins with a connector phrase, return the tokens after it, else ``None``.

    Exactly one phrase is stripped (longest match wins), so text that itself starts with a connector
    word — e.g. "para para mañana" → "para mañana" — keeps the rest intact.
    """
    normalized = [_normalize(token) for token in tokens]
    for phrase in CONNECTOR_PHRASES:
        if tuple(normalized[: len(phrase)]) == phrase:
            return tokens[len(phrase) :]
    return None


def _has_internal_connector(tokens: list[str]) -> bool:
    """True if a connector phrase begins *after* the first token with content following it.

    Such a mid-run boundary means a better (further-right) split exists, so a leading exact-template
    match that would leave this run as its recovered "text" is rejected in favour of that later
    split — e.g. exact "freezer" in "freezer for leftover para A1" leaves "leftover para A1", whose
    inner ``para`` signals the real boundary belongs to the longer "freezer-for-leftovers" prefix.
    """
    normalized = [_normalize(token) for token in tokens]
    for index in range(1, len(tokens)):
        for phrase in CONNECTOR_PHRASES:
            if (
                tuple(normalized[index : index + len(phrase)]) == phrase
                and tokens[index + len(phrase) :]
            ):
                return True
    return False


def _best_connector_split(
    tokens: list[str], templates: list[dict[str, Any]]
) -> tuple[float, dict[str, Any], str] | None:
    """The connector-boundary split whose prefix best matches a template, or ``None``.

    Used as the fuzzy fallback when no leading token-run is an *exact* template name, so a fuzzy
    template prefix ("pantri para …") can still be recovered. Every connector position with non-empty
    trailing text is a candidate. Considering *all* positions (not just the first connector) is what
    lets a multi-word name that itself contains a connector word be recovered: "freezer for leftover
    para A1" splits at ``para`` (prefix "freezer for leftover" ≈ "freezer-for-leftovers"), not at the
    earlier ``for``. Among candidates whose prefix matches *strongly* (≥ the whole cutoff) the
    **longest** prefix wins — the most specific template — so an exact match on a short overlapping
    name ("freezer") does not beat a near-exact match on a longer one ("freezer-for-leftovers").
    Otherwise the closest match wins.

    Returns ``(confidence, template, text)`` where confidence is the difflib ratio of the normalized
    prefix against the matched template name, so callers can gate on match strength.
    """
    normalized = [_normalize(token) for token in tokens]
    candidates: list[
        tuple[float, int, dict[str, Any], str]
    ] = []  # (conf, prefix_len, template, text)
    for index in range(1, len(tokens)):
        for phrase in CONNECTOR_PHRASES:
            if tuple(normalized[index : index + len(phrase)]) != phrase:
                continue
            text = " ".join(tokens[index + len(phrase) :]).strip()
            if text:
                prefix = _normalize(" ".join(tokens[:index]))
                template = _fuzzy_match_template(prefix, templates)
                if template is not None:
                    confidence = difflib.SequenceMatcher(
                        None, prefix, _normalize(template["name"])
                    ).ratio()
                    candidates.append((confidence, len(prefix), template, text))
            break
    if not candidates:
        return None
    strong = [c for c in candidates if c[0] >= WHOLE_TEMPLATE_MATCH_CUTOFF]
    chosen = (
        max(strong, key=lambda c: (c[1], c[0])) if strong else max(candidates, key=lambda c: c[0])
    )
    return (chosen[0], chosen[2], chosen[3])


def _split_template_and_text(
    spoken: str, templates: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve the template and recover any free text folded into the ``template`` wildcard.

    HA's ``recognize_best`` collapses "<template> <connector> <text>" into the single greedy
    trailing ``{template}`` wildcard for languages whose sentences lack a literal after it (see
    docs/voice-assist.md). Resolution order:

    1. If the *whole* utterance is exactly a template name, there is no free text — return it as is
       (so a legitimate multi-word template like ``freezer-dated`` is not split into ``freezer``).
    2. Take the longest leading token-run that is exactly a template name **followed by a connector
       phrase**; the tokens after it are the spoken text. Highest precision, so it runs before the
       whole-utterance match — otherwise a long name plus short text ("freezer for leftovers para
       A1") keeps the whole-string ratio above the cutoff and the text is silently dropped. It is
       overridden by the whole match (step 4) only when that name reads "<prefix> <connector>
       <tail>" *and* the recovered text fuzzy-matches <tail> — i.e. the connector is inside the name
       and the "text" is really a corrupted tail ("freezer for leftover" → "freezer-for-leftovers").
       Independent text ("regalo para uva" vs "regalo-para-navidad") keeps the split and its text.
       A short exact prefix whose recovered text still hides a connector boundary is rejected here,
       so a longer fuzzy prefix further right (step 3) can win.
    3. Otherwise take the best fuzzy connector split (:func:`_best_connector_split`, which prefers
       the longest strong-confidence prefix) when its match is strong (≥ the whole cutoff): this
       recovers text even when the prefix is an ASR/spelling variant of a multi-word name ("pantri
       para …", "freezer for leftover para A1"), and beats the whole match, which would drop the text.
    4. Otherwise, if the whole utterance is a *very close* match to a template name ("freezer for
       leftover" → "freezer-for-leftovers"), prefer it — a connector word inside a template name must
       not be read as a text boundary. The stricter cutoff keeps real "<template> <connector> <text>"
       commands (whose extra text lowers the whole-string ratio) from wrongly landing here.
    5. Otherwise fall back to a weaker fuzzy connector split, then to :func:`_fuzzy_match_template`
       on the whole utterance (no free text recovered).
    """
    by_normalized = {_normalize(t["name"]): t for t in templates}
    if _normalize(spoken) in by_normalized:
        return by_normalized[_normalize(spoken)], None

    tokens = spoken.split()
    # Longest exact leading template name + connector phrase → the text after it (highest precision).
    # ``boundary`` is the normalized "<prefix> <connector>" run, used to gate the whole-match override.
    exact_split: tuple[dict[str, Any], str, str] | None = None  # (template, boundary, text)
    for end in range(len(tokens) - 1, 0, -1):
        prefix = _normalize(" ".join(tokens[:end]))
        if prefix not in by_normalized:
            continue
        after_connector = _strip_leading_connector(tokens[end:])
        if after_connector is None:
            continue
        recovered = " ".join(after_connector).strip() or None
        # Reject a short exact prefix whose "text" hides a further connector boundary: a longer
        # (fuzzy) prefix split further right is the real intent (handled by _best_connector_split).
        if recovered is not None and not _has_internal_connector(after_connector):
            boundary = _normalize(" ".join(tokens[: len(tokens) - len(after_connector)]))
            exact_split = (by_normalized[prefix], boundary, recovered)
            break

    # Whole utterance as one (possibly ASR-variant) template name.
    whole_close = difflib.get_close_matches(
        _normalize(spoken), list(by_normalized), n=1, cutoff=WHOLE_TEMPLATE_MATCH_CUTOFF
    )
    whole_name = whole_close[0] if whole_close else None

    if exact_split is not None:
        template, boundary, recovered = exact_split
        # Override the exact split only when the connector sits *inside* the matched template name:
        # the whole match reads "<prefix> <connector> <tail>" AND the recovered text is really a
        # (possibly ASR-corrupted) rendering of that tail — e.g. "freezer for leftover" →
        # "freezer-for-leftovers" ("leftover" ≈ "leftovers"). When the text is independent of the
        # tail ("regalo para uva" vs "regalo-para-navidad", "freezer para lasagna" vs
        # "freezer-lasagna") the connector is a real boundary, so the exact split and its text stand.
        if whole_name is not None and whole_name.startswith(boundary + " "):
            tail = whole_name[len(boundary) + 1 :]
            if (
                difflib.SequenceMatcher(None, _normalize(recovered), tail).ratio()
                >= FUZZY_MATCH_CUTOFF
            ):
                return by_normalized[whole_name], None
        return template, recovered

    # A strong fuzzy connector split beats the whole-utterance match (step 4): an ASR variant of a
    # multi-word template prefix ("freezer for leftover para A1") must still recover the text.
    best_fuzzy = _best_connector_split(tokens, templates)
    if best_fuzzy is not None and best_fuzzy[0] >= WHOLE_TEMPLATE_MATCH_CUTOFF:
        return best_fuzzy[1], best_fuzzy[2]

    if whole_name is not None:
        return by_normalized[whole_name], None

    # Weaker connector split (prefix is a low-confidence fuzzy/substring match) as a last resort.
    if best_fuzzy is not None:
        return best_fuzzy[1], best_fuzzy[2]

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
