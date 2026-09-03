# SPDX-License-Identifier: MIT
"""The LabelitoPrint intent: voice-driven label printing via Assist.

Requires the user to copy the shipped ``custom_sentences/`` files into
``<config>/custom_sentences/<lang>/`` — integrations cannot bundle custom sentences.
"""

from __future__ import annotations

import difflib
import logging
from typing import TYPE_CHECKING, Any, ClassVar

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import intent

from .api import LabelitoApiError
from .const import (
    ATTR_ALIASES,
    ATTR_COPIES,
    ATTR_DRY_RUN,
    ATTR_FIELDS,
    ATTR_LANGUAGE,
    ATTR_TEMPLATE,
    CONF_VOICE_DRY_RUN,
    DEFAULT_VOICE_DRY_RUN,
    INTENT_PRINT,
)
from .services import async_execute_print, resolve_coordinator

if TYPE_CHECKING:
    from .coordinator import LabelitoCoordinator

_LOGGER = logging.getLogger(__name__)

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


def _raw_name(template: Any) -> str | None:
    """The entry's ``name`` if it is a non-empty string, else None — a pure shape check.

    Same rule as :func:`_alias_strings`, for the same reason: the catalog is an HTTP response from
    a service this integration does not own, so its shape is not ours to assume. Reading
    ``template["name"]`` on a malformed entry raises KeyError (no key), AttributeError (a non-string
    name reaching ``_normalize``) or TypeError (a list of plain strings instead of mappings) —
    measured, all three.

    The point is blast radius. Before this, ONE malformed entry raised out of the shared index and
    broke every spoken match for the WHOLE catalog; now it costs only itself and its neighbours
    still resolve.
    """
    if not isinstance(template, dict):
        return None
    name = template.get("name")
    if not isinstance(name, str) or not name:
        return None
    return name


def _template_name(template: Any) -> str | None:
    """The entry's name if it can serve as a SPOKEN form, else None.

    Beyond the shape check in :func:`_raw_name`, the name must survive normalization. A name made
    only of characters :func:`_normalize` strips — ``"..."``, ``"   "``, ``"-"``, ``"¿?"`` — reduces
    to the empty string, and an empty key is catastrophic in this index rather than merely useless:
    :func:`_split_template_and_text` tests exact membership first, so a punctuation-only slot from
    a speech-to-text engine matched it and printed that template; and the empty form reached the
    generated closed list as ``in: ""``. Aliases were already guarded against exactly this
    (:func:`_group_spoken_forms`); names were not.

    labelito's own save charset would reject such a name, but it does not constrain the ``name:``
    key of a YAML file placed in its templates directory by hand, so this is reachable. Excluding
    it costs nothing real: a name nobody can pronounce is not a name voice can use, and
    :func:`unsayable_template_names` reports it so the fix (a rename) is discoverable.
    """
    name = _raw_name(template)
    if name is None or not _normalize(name):
        return None
    return name


def unsayable_template_names(templates: list[dict[str, Any]]) -> list[str]:
    """Names that exist but cannot be a spoken form, because normalization leaves nothing.

    Reported by the ``write_voice_sentences`` service alongside the forms hassil could not take
    literally: both mean "this template exists and voice cannot reach it", and both are fixed the
    same way.
    """
    return sorted(
        {
            name
            for template in templates
            if (name := _raw_name(template)) is not None and not _normalize(name)
        }
    )


def _alias_strings(template: dict[str, Any]) -> list[str]:
    """The declared aliases of ``template``, ignoring anything that is not a list of strings.

    The catalog arrives as an HTTP response, so its shape is not ours to assume — and one
    malformed shape is actively dangerous rather than merely useless. A **string** where a list
    belongs iterates one "alias" per CHARACTER: ``aliases: "meal-prep"`` would register the spoken
    forms ``m``, ``e``, ``a``… and a one-letter form is a substring of almost any utterance, so
    :func:`_fuzzy_match_template`'s containment rule resolves it and a wrong physical label comes
    out of the printer. Measured, not theorised: with ``aliases: "not-a-list"``, the utterances
    "nada", "otro" and "lista" all resolved to that template.

    labelito validates aliases into a list of strings before serving them, but this integration
    talks to a service it does not own and cannot assume a version, a proxy, or a hand-rolled
    stand-in got that right.

    Logged at debug rather than warning on purpose: this runs on every spoken utterance (twice),
    so a warning would be a per-command log spam for a condition the user cannot act on — it is a
    bug in whatever served the catalog. The visible symptom is that the alias simply does nothing,
    and ``write_voice_sentences`` reports the spoken-form count.
    """
    aliases = template.get(ATTR_ALIASES)
    if aliases is None:
        return []
    if not isinstance(aliases, list):
        _LOGGER.debug(
            "Ignoring 'aliases' for template %r: expected a list, got %s",
            template.get("name"),
            type(aliases).__name__,
        )
        return []
    strings: list[str] = []
    for alias in aliases:
        if not isinstance(alias, str):
            _LOGGER.debug(
                "Ignoring alias %r for template %r: expected a string, got %s",
                alias,
                template.get("name"),
                type(alias).__name__,
            )
            continue
        strings.append(alias)
    return strings


def _group_spoken_forms(
    templates: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, dict[str, dict[str, Any]]]]:
    """Group the catalog by normalized name and by normalized alias, keeping every claimant.

    Two separate groupings, because the two tiers do not rank equally — see
    :func:`spoken_name_index`. Each maps a spoken form to the DISTINCT templates that claim it, so
    a catalog that somehow lists one template twice is not mistaken for a collision (the registry
    keys by name, so that should not arise) and two aliases on one template that normalize alike
    count once.
    """
    by_name: dict[str, dict[str, dict[str, Any]]] = {}
    for template in templates:
        name = _template_name(template)
        if name is None:
            continue
        by_name.setdefault(_normalize(name), {})[name] = template

    by_alias: dict[str, dict[str, dict[str, Any]]] = {}
    for template in templates:
        name = _template_name(template)
        if name is None:
            continue
        for alias in _alias_strings(template):
            key = _normalize(alias)
            # An alias that normalizes to nothing (punctuation only) would match every utterance
            # via the substring rule; one that collides with ANY spoken name — including a name
            # form that is itself ambiguous and therefore dropped below — must not resolve either,
            # or adding an alias to one template could hijack another template's own name.
            if not key or key in by_name:
                continue
            by_alias.setdefault(key, {})[name] = template
    return by_name, by_alias


def spoken_name_index(templates: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Map every spoken form of a template — its name and its aliases — to that template.

    :func:`_normalize` is lossy — it folds ``-``/``_`` to spaces and strips sentence punctuation —
    so distinct names can share a key: ``pantry-1`` and ``pantry_1`` both become "pantry 1", and
    both are legal saved names (labelito's save charset allows ``-`` and ``_``). A plain dict
    comprehension let whichever entry came last in the catalog own that key, so a spoken "pantry
    one" could resolve to, and print, the other template.

    An ambiguous key resolves to nothing instead. The caller then speaks the unknown-template
    prompt with the real names, which is the only answer that cannot produce the wrong label —
    there is genuinely no way to tell which of the two was meant. Ambiguous forms are also kept out
    of the substring and fuzzy passes, for the same reason.

    **Names outrank aliases.** A template's ``aliases`` (labelito's optional per-template list of
    alternative spoken names, for the hyphen nobody says aloud and the other word for the same
    thing) are added only for forms no name claims. Without that precedence, an alias on one
    template could resolve — and print — in place of another template's actual name, which is the
    one thing a matcher must never do. Within the alias tier the same ambiguity rule applies: a
    form two templates alias resolves to neither.

    Aliases are resolved here, in the shared index, rather than only in the generated closed-list
    grammar (:mod:`.voice_sentences`) — so an alias still works when that file is stale, absent, or
    was never generated, and the two paths can never disagree about what a name means.
    """
    by_name, by_alias = _group_spoken_forms(templates)
    index = {
        key: next(iter(claimants.values()))
        for key, claimants in by_name.items()
        if len(claimants) == 1
    }
    index.update(
        {
            key: next(iter(claimants.values()))
            for key, claimants in by_alias.items()
            if len(claimants) == 1
        }
    )
    return index


def resolvable_spoken_forms(templates: list[dict[str, Any]]) -> dict[str, str]:
    """Map each spoken form to the canonical template name, for the forms this handler can resolve.

    The vocabulary :mod:`.voice_sentences` compiles into a closed grammar. Filtered to forms that
    ROUND-TRIP: a grammar match hands the handler the canonical name as if it had been spoken, and
    :func:`spoken_name_index` resolves it again — so a form whose own name no longer resolves would
    be matched by the grammar and then reported as an unknown template.

    That is reachable through an alias. Templates named ``pantry-1`` and ``pantry_1`` make the name
    form "pantry 1" ambiguous, so it is dropped; an alias on one of them stays unique, and emitting
    it would hand back a name this index has deliberately stopped resolving. Deciding that here
    keeps every rule about what a spoken name means in one module, and leaves the generator with
    only its own concerns (grammar syntax, YAML, files).
    """
    index = spoken_name_index(templates)
    return {
        spoken: str(template["name"])
        for spoken, template in index.items()
        if index.get(_normalize(str(template["name"]))) is template
    }


def ambiguous_spoken_forms(templates: list[dict[str, Any]]) -> list[str]:
    """The spoken forms :func:`spoken_name_index` dropped because more than one template claims them.

    Reported by the ``write_voice_sentences`` service: a dropped form is silently unusable by
    voice, and the only fix is renaming a template or an alias — which the user can only do if
    something tells them which form is contested.
    """
    by_name, by_alias = _group_spoken_forms(templates)
    return sorted(
        key
        for group in (by_name, by_alias)
        for key, claimants in group.items()
        if len(claimants) > 1
    )


def _fuzzy_match_template(spoken: str, templates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match a spoken template name against the catalog: exact, then substring, then fuzzy close.

    Substring containment is checked before the generic ``get_close_matches`` pass: full containment
    (the spoken value is a substring of a template name, or vice versa) is a stronger signal than a
    fuzzy ratio, so "gift" resolves to a ``gift-box`` template rather than a coincidental typo
    neighbour like ``grift``. The longest overlapping name wins, so an overlapping catalog (e.g.
    freezer / freezer-dated) resolves to the more specific template regardless of catalog order.
    """
    by_normalized = spoken_name_index(templates)
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
    by_normalized = spoken_name_index(templates)
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
