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
    CONF_VOICE_DRY_RUN,
    DEFAULT_VOICE_DRY_RUN,
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
        # Spoken when the voice_dry_run option is on. Worded so the absence of a label is the
        # first thing heard: a silent success would be indistinguishable from a printer fault.
        "dry_run": "Dry run: nothing printed. The {template} label is ready.",
        "dry_run_text": "Dry run: nothing printed. The {template} label for {text} is ready.",
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
        "dry_run": "Prueba en seco: no he impreso nada. La etiqueta de {template} está lista.",
        "dry_run_text": (
            "Prueba en seco: no he impreso nada. La etiqueta de {template} para {text} está lista."
        ),
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

# Sentence punctuation a speech-to-text engine puts around an utterance. Streaming ASR models emit
# mixed-case, punctuated text, so a spoken template name arrives as "pantry." whenever it lands at
# the end of the sentence (which it always does for the no-text sentence, where {template} is the
# trailing wildcard). Stripped from both sides of every comparison in _normalize so the exact match
# still wins; without it "pantry." resolves only because _fuzzy_match_template's substring rule
# happens to rescue it, and a name short enough to fall under FUZZY_MATCH_CUTOFF would not resolve
# at all.
SENTENCE_PUNCTUATION = ".,;:!?¡¿\"'«»…"

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
    return name.lower().replace("-", " ").replace("_", " ").strip(SENTENCE_PUNCTUATION + " ")


def _index_by_normalized(templates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index the catalog by normalized name, dropping any key more than one template claims.

    :func:`_normalize` is lossy — it folds ``-``/``_`` to spaces and strips sentence punctuation —
    so distinct names can share a key: ``pantry-1`` and ``pantry_1`` both become "pantry 1", and
    both are legal saved names (labelito's save charset allows ``-`` and ``_``). A plain dict
    comprehension let whichever entry came last in the catalog own that key, so a spoken "pantry
    one" could resolve to, and print, the other template.

    An ambiguous key resolves to nothing instead. The caller then speaks the unknown-template
    prompt with the real names, which is the only answer that cannot produce the wrong label —
    there is genuinely no way to tell which of the two was meant. Ambiguous names are also kept out
    of the substring and fuzzy passes, for the same reason.

    Grouping is by DISTINCT raw name, so a catalog that somehow lists one template twice is not
    mistaken for a collision (the registry keys by name, so this should not arise).
    """
    grouped: dict[str, dict[str, dict[str, Any]]] = {}
    for template in templates:
        grouped.setdefault(_normalize(template["name"]), {})[template["name"]] = template
    return {key: next(iter(by_raw.values())) for key, by_raw in grouped.items() if len(by_raw) == 1}


def _fuzzy_match_template(spoken: str, templates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a spoken template name against the catalog: exact, then substring, then fuzzy close.

    Substring containment is checked before the generic ``get_close_matches`` pass: full containment
    (the spoken value is a substring of a template name, or vice versa) is a stronger signal than a
    fuzzy ratio, so "gift" resolves to a ``gift-box`` template rather than a coincidental typo
    neighbour like ``grift``. The longest overlapping name wins, so an overlapping catalog (e.g.
    freezer / freezer-dated) resolves to the more specific template regardless of catalog order.
    """
    by_normalized = _index_by_normalized(templates)
    wanted = _normalize(spoken)
    if not wanted:
        # Nothing survived normalization — a punctuation-only slot ("." from an ASR that heard no
        # template), or an empty prefix before a connector. This MUST return before the substring
        # pass: `"" in name` holds for every template, so containment would hand back whichever
        # catalog name is longest, and the caller would print it. Guarded here, at the single
        # place that compares against the catalog, rather than at each call site — the raw string
        # can be non-empty while normalizing to nothing (", para queso" has prefix ","), so a
        # caller-side truthiness check does not cover it.
        return None
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
    connectors — only the first is consumed).

    Every branch below is annotated with the utterance that reaches it, since the slot values are
    what ``recognize_best`` produced rather than anything a user typed. Assume a catalog of
    ``pantry`` / ``freezer`` / ``freezer-dated`` / ``queso``, and read `→` as "resolves to".

    1. **Exact name** — "pantry" → ``pantry`` + no text. Wins before anything else, so a name that
       itself contains a connector word survives: "gift for christmas" →
       ``gift-for-christmas``, not ``gift`` + text "christmas".

    2. **Connector boundary, prefix resolves** — the normal recovery.
       "pantry para sopa de tomate" → ``pantry`` + "sopa de tomate".
       "pantri que diga lasaña" → ``pantry`` + "lasaña" (ASR variant, fuzzily matched).
       Only the FIRST connector is the boundary, so the text may contain more:
       "pantry para para mañana" → ``pantry`` + "para mañana".

    3. **Connector boundary, prefix does not resolve — a miss, deliberately not a fallback.**
       "nevera para queso manchego" with no ``nevera`` template → nothing.
       Falling through to a whole-utterance match here would let the substring rule in
       :func:`_fuzzy_match_template` return ``queso`` — a template merely *named inside the
       dictated text* — with no text, so a template with no required fields would print the wrong
       label and confirm it, and one with required fields would ask for text nobody was offering.

    4. **Connector boundary with a prefix that names nothing — the same miss.**
       "para queso manchego", which is what "haz una etiqueta para queso manchego" collapses to
       once the optional "de" is omitted (likewise "que diga queso manchego"). Also
       ", para queso manchego", where the prefix is punctuation an ASR emitted and normalizes to
       nothing. Neither names a template, so neither resolves — :func:`_fuzzy_match_template`
       rejects a normalized-empty value outright, because ``"" in name`` is true for every
       template and its substring rule would otherwise return the longest catalog name *together
       with* the dictated text: a plausible-looking wrong label that prints even for a
       required-field template.

    5. **No connector anywhere** — the whole utterance is a template name, ASR noise included, so
       the substring rule is the intended rescue: "freezer dated uno dos tres" → ``freezer-dated``.
    """
    by_normalized = _index_by_normalized(templates)
    if _normalize(spoken) in by_normalized:
        return by_normalized[_normalize(spoken)], None  # case 1

    tokens = spoken.split()
    normalized = [_normalize(token) for token in tokens]
    # From index 0, not 1: a connector opening the slot (case 4) still marks a boundary, and
    # skipping it would misfile the utterance as "no connector" and drop it into case 5 — the very
    # fallback cases 3 and 4 exist to avoid.
    for index in range(len(tokens)):
        phrase = next(
            (p for p in CONNECTOR_PHRASES if tuple(normalized[index : index + len(p)]) == p),
            None,
        )
        if phrase is None:
            continue
        # A prefix that names no template cannot resolve, including one that is empty or is pure
        # punctuation (", para queso"). Both are handled by _fuzzy_match_template's own
        # normalized-empty guard, so there is exactly one place that decides it.
        template = _fuzzy_match_template(" ".join(tokens[:index]), templates)
        if template is not None:
            return template, " ".join(tokens[index + len(phrase) :]).strip() or None  # case 2
        return None, None  # cases 3 and 4

    return _fuzzy_match_template(spoken, templates), None  # case 5


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

        # A spoken print has no per-call dry_run (the sentence grammar carries only template and
        # text), so the choice lives on the config entry. labelito still renders and validates a
        # dry run, so a template miss or a missing required field is reported exactly as it would
        # be for a real print — only the tape is spared. ``config_entry`` is typed optional on the
        # coordinator base class; with none to read, the default (print for real) applies.
        entry = coordinator.config_entry
        dry_run = bool(
            entry is not None and entry.options.get(CONF_VOICE_DRY_RUN, DEFAULT_VOICE_DRY_RUN)
        )
        request: dict[str, Any] = {
            ATTR_TEMPLATE: template["name"],
            ATTR_FIELDS: {},
            ATTR_COPIES: 1,
            ATTR_DRY_RUN: dry_run,
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

        prefix = "dry_run" if dry_run else "printed"
        key = f"{prefix}_text" if text else prefix
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
