"""Tests for the LabelitoPrint Assist intent handler.

The regression these lock down: HA's ``recognize_best`` folds the whole Spanish utterance into the
greedy trailing ``{template}`` wildcard (see docs/voice-assist.md), so the handler receives
``template="pantry para sopa de tomate"`` and no ``text`` slot. The handler must recover the free
text, map it to the template's first required field, and — when labelito rejects a print for a
missing required field — turn that server 422 into an actionable spoken prompt.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import intent

from custom_components.labelito.api import LabelitoApiError
from custom_components.labelito.const import ATTR_DRY_RUN, CONF_VOICE_DRY_RUN, INTENT_PRINT
from custom_components.labelito.intents import (
    LabelitoPrintIntentHandler,
    _split_template_and_text,
    spoken_name_index,
)

from .const import MOCK_TEMPLATES


def _api_error(status: int, detail: Any, message: str) -> ServiceValidationError:
    """A ServiceValidationError chained from a LabelitoApiError, as services.py raises it."""
    err = ServiceValidationError(message)
    err.__cause__ = LabelitoApiError(status, detail)
    return err


# A template with no required fields: printing it without text is legal (no needs_text error).
NO_REQUIRED_TEMPLATE: dict[str, Any] = {
    "name": "blank",
    "description": "Blank label",
    "label": "62",
    "rotate": 0,
    "fields": {"required": [], "optional": ["note"]},
    "media": None,
    "uses_seq": False,
}


def _make_coordinator(
    templates: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> Mock:
    catalog = list(templates if templates is not None else MOCK_TEMPLATES)
    coordinator = Mock()
    coordinator.async_get_templates = AsyncMock(return_value=catalog)
    coordinator.template_names = Mock(return_value=[t["name"] for t in catalog])
    # A real dict, not a Mock attribute: the handler reads options.get(CONF_VOICE_DRY_RUN, ...),
    # and an auto-created Mock would return a truthy Mock and silently dry-run every test.
    coordinator.config_entry.options = dict(options or {})
    return coordinator


def _make_intent(hass: HomeAssistant, slots: dict[str, Any], language: str) -> intent.Intent:
    return intent.Intent(
        hass,
        platform="test",
        intent_type=INTENT_PRINT,
        slots={name: {"value": value} for name, value in slots.items()},
        text_input=None,
        context=Context(),
        language=language,
    )


async def _handle(
    hass: HomeAssistant,
    slots: dict[str, Any],
    *,
    language: str = "es",
    coordinator: Mock | None = None,
    execute: AsyncMock | None = None,
) -> tuple[intent.IntentResponse, AsyncMock]:
    coordinator = coordinator or _make_coordinator()
    execute = execute or AsyncMock()
    handler = LabelitoPrintIntentHandler()
    with (
        patch(
            "custom_components.labelito.intents.resolve_coordinator",
            return_value=coordinator,
        ),
        patch("custom_components.labelito.intents.async_execute_print", new=execute),
    ):
        response = await handler.async_handle(_make_intent(hass, slots, language))
    return response, execute


def _speech(response: intent.IntentResponse) -> str:
    return response.speech["plain"]["speech"]


def _printed_request(execute: AsyncMock) -> dict[str, Any]:
    execute.assert_awaited_once()
    return execute.await_args.args[1]


# --- text recovery from the over-captured {template} wildcard --------------------------------


async def test_recovers_text_from_para_overcapture(hass: HomeAssistant) -> None:
    """recognize_best gives template='pantry para sopa de tomate'; recover the text into title."""
    response, execute = await _handle(hass, {"template": "pantry para sopa de tomate"})
    request = _printed_request(execute)
    assert request["template"] == "pantry"
    assert request["fields"] == {"title": "sopa de tomate"}
    assert _speech(response) == "He imprimido una etiqueta de pantry para sopa de tomate."


async def test_recovers_text_from_que_diga_overcapture(hass: HomeAssistant) -> None:
    _, execute = await _handle(hass, {"template": "pantry que diga sopa de tomate"})
    assert _printed_request(execute)["fields"] == {"title": "sopa de tomate"}


async def test_normal_slots_still_map_to_required_field(hass: HomeAssistant) -> None:
    """When Assist already split the slots, the explicit text slot is used verbatim."""
    response, execute = await _handle(hass, {"template": "pantry", "text": "sopa de tomate"})
    assert _printed_request(execute)["fields"] == {"title": "sopa de tomate"}
    assert _speech(response) == "He imprimido una etiqueta de pantry para sopa de tomate."


async def test_english_slots_unaffected(hass: HomeAssistant) -> None:
    """English sentences anchor {template} with 'label', so its slots arrive already split."""
    response, execute = await _handle(
        hass, {"template": "pantry", "text": "tomato soup"}, language="en"
    )
    assert _printed_request(execute)["fields"] == {"title": "tomato soup"}
    assert _speech(response) == "Printed a pantry label for tomato soup."


# --- graceful handling when there is no text ------------------------------------------------


async def test_missing_required_422_speaks_needs_text(hass: HomeAssistant) -> None:
    """labelito is authoritative: its missing-required 422 becomes the actionable prompt."""
    execute = AsyncMock(
        side_effect=_api_error(
            422,
            {"msg": "Missing required fields", "missing_required": ["title"]},
            "Missing required fields: title",
        )
    )
    response, execute = await _handle(hass, {"template": "pantry"}, execute=execute)
    execute.assert_awaited_once()
    assert _speech(response) == "Necesito el texto para la etiqueta de pantry."


async def test_missing_required_422_speaks_needs_text_english(hass: HomeAssistant) -> None:
    execute = AsyncMock(
        side_effect=_api_error(
            422,
            {"msg": "Missing required fields", "missing_required": ["title"]},
            "Missing required fields: title",
        )
    )
    response, execute = await _handle(hass, {"template": "pantry"}, language="en", execute=execute)
    assert _speech(response) == "I need the text to put on the pantry label."


async def test_missing_required_with_text_supplied_surfaces_field(hass: HomeAssistant) -> None:
    """Text was given but a second required field is still missing: name it, don't re-ask for text.

    The intent fills only the first required field, so a multi-required-field template can still
    422; the user should hear which field is missing, not a misleading "I need the text".
    """
    execute = AsyncMock(
        side_effect=_api_error(
            422,
            {"msg": "Missing required fields", "missing_required": ["subtitle"]},
            "Missing required fields: subtitle",
        )
    )
    response, _ = await _handle(
        hass, {"template": "pantry", "text": "tomato soup"}, execute=execute
    )
    speech = _speech(response)
    assert speech.startswith("No he podido imprimir la etiqueta:")
    assert "subtitle" in speech


async def test_other_print_error_speaks_failed(hass: HomeAssistant) -> None:
    """A non-missing-required failure (e.g. a media mismatch) surfaces verbatim, not needs_text."""
    execute = AsyncMock(
        side_effect=_api_error(
            409,
            {"media_loaded": "62mm continuous", "media_required": "29x90mm die-cut"},
            "The loaded roll is 62mm continuous but the template needs 29x90mm die-cut",
        )
    )
    response, _ = await _handle(hass, {"template": "pantry", "text": "x"}, execute=execute)
    speech = _speech(response)
    assert speech.startswith("No he podido imprimir la etiqueta:")
    assert "62mm continuous" in speech


async def test_no_required_fields_prints_without_text(hass: HomeAssistant) -> None:
    """A template with no required fields prints fine with empty fields — no needs_text."""
    coordinator = _make_coordinator([NO_REQUIRED_TEMPLATE])
    response, execute = await _handle(hass, {"template": "blank"}, coordinator=coordinator)
    request = _printed_request(execute)
    assert request["fields"] == {}
    assert _speech(response) == "He imprimido una etiqueta de blank."


async def test_unknown_template_lists_available(hass: HomeAssistant) -> None:
    response, execute = await _handle(hass, {"template": "banana"})
    execute.assert_not_awaited()
    speech = _speech(response)
    assert "No conozco" in speech
    assert "pantry" in speech


async def test_template_miss_forces_catalog_refresh_then_prints(hass: HomeAssistant) -> None:
    """A miss on the cached catalog forces one refresh; a template new in the fresh catalog prints."""
    coordinator = _make_coordinator()
    coordinator.async_get_templates.side_effect = [
        list(MOCK_TEMPLATES),
        [*MOCK_TEMPLATES, {"name": "seasonal"}],
    ]

    _, execute = await _handle(hass, {"template": "seasonal"}, coordinator=coordinator)

    assert _printed_request(execute)["template"] == "seasonal"
    assert coordinator.async_get_templates.await_count == 2
    assert coordinator.async_get_templates.await_args_list[1].kwargs == {"force_refresh": True}


async def test_unknown_template_named_inside_text_is_not_printed(hass: HomeAssistant) -> None:
    """An unknown template before a connector must not resolve to one named in the dictated text.

    "haz una etiqueta de nevera para queso manchego" with no ``nevera`` template used to print a
    ``queso`` label with no text at all — the substring fallback matched the word inside the
    dictated text. The user gets the unknown-template prompt and nothing is printed.
    """
    coordinator = _make_coordinator(
        templates=[{"name": "queso", "fields": {"required": ["title"], "optional": []}}]
    )

    response, execute = await _handle(
        hass, {"template": "nevera para queso manchego"}, coordinator=coordinator
    )

    execute.assert_not_awaited()
    assert "No conozco ninguna plantilla" in _speech(response)
    assert "queso" in _speech(response)  # still lists what IS available


# --- the voice_dry_run option ----------------------------------------------------------------


async def test_voice_dry_run_off_by_default_prints_for_real(hass: HomeAssistant) -> None:
    _, execute = await _handle(hass, {"template": "pantry para sopa de tomate"})
    assert _printed_request(execute)[ATTR_DRY_RUN] is False


@pytest.mark.parametrize(
    ("language", "slots", "expected_fragment"),
    [
        ("es", {"template": "pantry para sopa de tomate"}, "Prueba en seco"),
        ("es", {"template": "pantry"}, "Prueba en seco"),
        ("en", {"template": "pantry", "text": "tomato soup"}, "Dry run"),
    ],
)
async def test_voice_dry_run_sends_flag_and_says_nothing_printed(
    hass: HomeAssistant, language: str, slots: dict[str, Any], expected_fragment: str
) -> None:
    """The option must both reach labelito AND be audible: a silent dry run is indistinguishable
    from a printer that quietly failed."""
    coordinator = _make_coordinator(options={CONF_VOICE_DRY_RUN: True})

    response, execute = await _handle(hass, slots, language=language, coordinator=coordinator)

    assert _printed_request(execute)[ATTR_DRY_RUN] is True
    assert expected_fragment in _speech(response)


# --- the split helper in isolation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "expected_name", "expected_text"),
    [
        ("pantry para sopa de tomate", "pantry", "sopa de tomate"),
        ("pantry que diga sopa de tomate", "pantry", "sopa de tomate"),
        ("pantry", "pantry", None),
        ("banana", None, None),
        # Streaming ASR models punctuate their output, and the no-text sentence puts {template}
        # last, so the template name reliably arrives with a trailing period.
        ("pantry.", "pantry", None),
        ("¡pantry!", "pantry", None),
        # A punctuated connector still marks the boundary (_normalize runs per token). This is the
        # case the strip actually buys: without it "diga," never equals "diga", no boundary is
        # found, and the whole utterance falls through to a substring match on "pantry" — losing
        # the dictated text entirely. The trailing-period cases above are rescued by the substring
        # rule either way; they document intent rather than guard a behaviour change.
        ("pantry, que diga sopa de tomate", "pantry", "sopa de tomate"),
        ("pantry que diga, sopa de tomate", "pantry", "sopa de tomate"),
        ("pantry that says: cheese", "pantry", "cheese"),
    ],
)
def test_split_template_and_text(
    spoken: str, expected_name: str | None, expected_text: str | None
) -> None:
    template, text = _split_template_and_text(spoken, list(MOCK_TEMPLATES))
    assert (template["name"] if template else None) == expected_name
    assert text == expected_text


# Catalog where one template name is a prefix of another (freezer vs freezer-dated).
_OVERLAP_CATALOG = [{"name": "freezer"}, {"name": "freezer-dated"}]


# These lock down _split_template_and_text, which assumes template names contain NO connector words
# (para/for/que diga/that says): the FIRST connector phrase is the template/text boundary.
@pytest.mark.parametrize(
    ("spoken", "templates", "expected_name", "expected_text"),
    [
        # Exact multi-word template with no connector present — the whole utterance is the name.
        ("freezer dated", _OVERLAP_CATALOG, "freezer-dated", None),
        # ...and a real overcapture on that multi-word template still recovers the text.
        ("freezer dated para lasagna", _OVERLAP_CATALOG, "freezer-dated", "lasagna"),
        # An exactly-spoken template name wins even when it CONTAINS a connector word: "gift for
        # christmas" is the whole template, not "gift" + text "christmas".
        (
            "gift for christmas",
            [{"name": "gift"}, {"name": "gift-for-christmas"}],
            "gift-for-christmas",
            None,
        ),
        # Only the FIRST connector is the boundary: text may itself contain connector words.
        ("pantry para para mañana", [{"name": "pantry"}], "pantry", "para mañana"),
        # A trailing word that is not a connector is not mistaken for text (no split without one).
        ("freezer lasagna", [{"name": "freezer"}], "freezer", None),
        # ASR/spelling variant of the template before a connector still fuzzy-resolves + recovers.
        ("pantri para sopa de tomate", [{"name": "pantry"}], "pantry", "sopa de tomate"),
        ("freezr que diga lasaña", [{"name": "freezer"}], "freezer", "lasaña"),
        # A genuine short text after the connector is recovered, not swallowed.
        ("pantry para si", [{"name": "pantry"}], "pantry", "si"),
        # Multi-word template name (no connector) + connector + short text.
        (
            "long template name para ok",
            [{"name": "long-template-name"}],
            "long-template-name",
            "ok",
        ),
        # Overlapping names: the first connector "para" is the boundary, so "freezer para lasagna"
        # is template "freezer" + text "lasagna" (not the longer "freezer-lasagna").
        (
            "freezer para lasagna",
            [{"name": "freezer"}, {"name": "freezer-lasagna"}],
            "freezer",
            "lasagna",
        ),
        # Substring fallback in _fuzzy_match_template prefers the longest overlapping name over
        # catalog order: "freezer" is listed first but "freezer-dated" is the more specific match.
        # No connector is present here, which is what keeps that whole-utterance rescue legal.
        (
            "freezer dated uno dos tres cuatro",
            [{"name": "freezer"}, {"name": "freezer-dated"}],
            "freezer-dated",
            None,
        ),
        # REGRESSION (silent wrong label): a connector DID mark a boundary but "nevera" is not a
        # template, so this must be reported as a miss. Without the saw_connector guard the
        # whole-utterance fallback ran and its substring rule matched "queso" — a template merely
        # named inside the dictated text — returning text=None, i.e. a queso label with no text.
        (
            "nevera para queso manchego",
            [{"name": "queso"}, {"name": "congelador"}],
            None,
            None,
        ),
        # Same shape in English, and with the mentioned template at the very end of the text.
        (
            "unknown that says cheese",
            [{"name": "cheese"}],
            None,
            None,
        ),
        # REGRESSION (silent wrong label, leading connector): the slot OPENS with a connector, so
        # there is no name before the boundary at all. Reached by omitting the optional "de":
        # "haz una etiqueta para queso manchego" collapses to exactly this — verified against
        # hassil in test_hassil_leading_connector_collapses_into_template below. The scan used to
        # start at index 1, which misfiled this as "no connector" and dropped it into the
        # whole-utterance fallback, where the substring rule returned `queso` with no text.
        ("para queso manchego", [{"name": "queso"}], None, None),
        # Both connector forms, since the two-word one is a separate code path.
        ("que diga queso manchego", [{"name": "queso"}], None, None),
        ("for cheese with ham", [{"name": "cheese"}], None, None),
        ("that says cheese with ham", [{"name": "cheese"}], None, None),
        # An empty prefix must not be handed to _fuzzy_match_template at all: "" is a substring of
        # every name, so its containment rule would return an arbitrary template. With more than
        # one candidate the longest would win, which is why this case names two.
        ("para algo", [{"name": "queso"}, {"name": "freezer-dated"}], None, None),
        # REGRESSION (arbitrary template, WITH the dictated text): a prefix that is non-empty as a
        # string but normalizes to nothing. Introduced by the punctuation stripping itself — the
        # guard was on the raw prefix, and "," is truthy. This is the worst shape of the family:
        # the text after the connector IS recovered, so the print goes through even for a
        # required-field template, producing a plausible-looking label under the wrong template.
        (
            ", para queso manchego",
            [{"name": "queso"}, {"name": "freezer-dated"}],
            None,
            None,
        ),
        (
            ". que diga lasaña",
            [{"name": "queso"}, {"name": "freezer-dated"}],
            None,
            None,
        ),
        # Punctuation-only slots, i.e. an ASR that heard no template name at all.
        (".", [{"name": "queso"}, {"name": "freezer-dated"}], None, None),
        ("...", [{"name": "queso"}, {"name": "freezer-dated"}], None, None),
        ("¿?", [{"name": "queso"}, {"name": "freezer-dated"}], None, None),
        # AMBIGUOUS CATALOG: _normalize is lossy, so two distinct names can share a key. Neither
        # can be chosen, so both resolve to nothing rather than letting catalog order decide.
        # This pair is the PRE-EXISTING case — "-" and "_" were already folded to spaces before
        # the punctuation stripping, and both are legal saved names, so it is the reachable one.
        ("pantry 1", [{"name": "pantry-1"}, {"name": "pantry_1"}], None, None),
        ("pantry-1", [{"name": "pantry-1"}, {"name": "pantry_1"}], None, None),
        # And the pair the punctuation stripping adds. Only reachable via a hand-authored YAML,
        # since labelito's save charset rejects "!" outright.
        ("pantry", [{"name": "pantry"}, {"name": "pantry!"}], None, None),
        # An unambiguous catalog is untouched: folding still resolves a spoken name to its file.
        ("freezer dated", [{"name": "freezer-dated"}, {"name": "pantry"}], "freezer-dated", None),
    ],
)
def test_split_template_and_text_prefix_overlap(
    spoken: str,
    templates: list[dict[str, Any]],
    expected_name: str | None,
    expected_text: str | None,
) -> None:
    template, text = _split_template_and_text(spoken, templates)
    assert (template["name"] if template else None) == expected_name
    assert text == expected_text


# --- hassil-level regression: documents WHY the handler recovery is needed ------------------


def test_hassil_recognize_best_folds_spanish_text_into_template() -> None:
    """Feed the shipped sentence YAML through recognize_best exactly as HA does.

    Documents the root cause so a future grammar change can be re-validated: Spanish collapses
    everything into ``template`` (no ``text``), while English anchors on "label" and extracts text.
    Skipped when hassil is not installed in the test env.
    """
    hassil = pytest.importorskip("hassil")
    import pathlib

    import yaml
    from hassil.recognize import recognize_best

    repo = pathlib.Path(__file__).resolve().parent.parent

    def best(lang: str, utterance: str) -> dict[str, str] | None:
        data = yaml.safe_load((repo / "custom_sentences" / lang / "labelito.yaml").read_text())
        intents = hassil.Intents.from_dict(data)
        result = recognize_best(
            utterance,
            intents,
            best_metadata_key="hass_custom_sentence",
            best_slot_name="name",
        )
        return None if result is None else {k: v.text for k, v in result.entities.items()}

    es = best("es", "imprime una etiqueta de pantry para sopa de tomate")
    assert es == {"template": "pantry para sopa de tomate"}  # text folded in — no 'text' slot

    en = best("en", "print a pantry label for tomato soup")
    assert en == {"template": "pantry", "text": "tomato soup"}  # anchored, text extracted


def test_hassil_leading_connector_collapses_into_template() -> None:
    """The shipped Spanish grammar really can hand the handler a slot that OPENS with a connector.

    The no-text sentence is "imprime una etiqueta [de] {template}" — with the optional "de" left
    out, everything after "etiqueta" lands in the wildcard, connector included. This is the input
    that makes the empty-prefix case (case 4 in _split_template_and_text) reachable rather than
    theoretical, so it is pinned against the real grammar instead of assumed.
    """
    hassil = pytest.importorskip("hassil")
    import pathlib

    import yaml
    from hassil.recognize import recognize_best

    repo = pathlib.Path(__file__).resolve().parent.parent
    data = yaml.safe_load((repo / "custom_sentences" / "es" / "labelito.yaml").read_text())
    intents = hassil.Intents.from_dict(data)

    def best(utterance: str) -> dict[str, str] | None:
        result = recognize_best(
            utterance,
            intents,
            best_metadata_key="hass_custom_sentence",
            best_slot_name="name",
        )
        return None if result is None else {k: v.text for k, v in result.entities.items()}

    assert best("haz una etiqueta para queso manchego") == {"template": "para queso manchego"}
    assert best("haz una etiqueta que diga queso manchego") == {
        "template": "que diga queso manchego"
    }


async def test_leading_connector_prints_nothing(hass: HomeAssistant) -> None:
    """The severity of the leading-connector case: it used to PRINT, not just misreport.

    With a template that has no required fields, labelito accepts a print with no fields, so the
    old whole-utterance fallback resolved `queso`, sent the job, and confirmed "He imprimido una
    etiqueta de queso" — a wrong label with a cheerful confirmation. Nothing may be printed here.
    """
    coordinator = _make_coordinator(
        templates=[{"name": "queso", "fields": {"required": [], "optional": []}}]
    )

    response, execute = await _handle(
        hass, {"template": "para queso manchego"}, coordinator=coordinator
    )

    execute.assert_not_awaited()
    assert "No conozco ninguna plantilla" in _speech(response)


async def test_punctuation_prefix_prints_nothing(hass: HomeAssistant) -> None:
    """The worst shape of the named-in-text family: it prints WITH text, so nothing stops it.

    A prefix of pure punctuation normalizes to nothing but is a non-empty string, so the raw-prefix
    guard let it through to the substring rule, which returns the longest catalog name. The text
    after the connector is recovered normally, so the print is not saved by a missing-field 422
    either: the user asked for nothing and would get a `freezer-dated` label reading
    "queso manchego".
    """
    coordinator = _make_coordinator(
        templates=[
            {"name": "queso", "fields": {"required": ["title"], "optional": []}},
            {"name": "freezer-dated", "fields": {"required": ["title"], "optional": []}},
        ]
    )

    response, execute = await _handle(
        hass, {"template": ", para queso manchego"}, coordinator=coordinator
    )

    execute.assert_not_awaited()
    assert "No conozco ninguna plantilla" in _speech(response)


async def test_punctuation_only_template_prints_nothing(hass: HomeAssistant) -> None:
    """A slot of pure punctuation names no template, so it must not resolve to the longest one."""
    coordinator = _make_coordinator(
        templates=[{"name": "freezer-dated", "fields": {"required": [], "optional": []}}]
    )

    response, execute = await _handle(hass, {"template": "."}, coordinator=coordinator)

    execute.assert_not_awaited()
    assert "No conozco ninguna plantilla" in _speech(response)


# --- aliases: alternative spoken names, resolved in the SHARED index -------------------------
#
# Aliases are resolved here and not only in the generated closed-list grammar, so an alias keeps
# working when that file is stale, absent, or was never generated — and so the two paths can never
# disagree about what a spoken name means.

ALIASED_CATALOG: list[dict[str, Any]] = [
    {**NO_REQUIRED_TEMPLATE, "name": "congelador", "aliases": ["congelado"]},
    {**NO_REQUIRED_TEMPLATE, "name": "nevera"},
]


@pytest.mark.parametrize("spoken", ["congelado", "Congelado.", "congelado"])
def test_an_alias_resolves_to_its_template(spoken: str) -> None:
    template, text = _split_template_and_text(spoken, ALIASED_CATALOG)
    assert template is not None
    assert template["name"] == "congelador"
    assert text is None


def test_an_alias_resolves_with_recovered_text() -> None:
    """The connector split runs against the alias exactly as it does against a name."""
    template, text = _split_template_and_text("congelado para lasaña", ALIASED_CATALOG)
    assert template is not None
    assert template["name"] == "congelador"
    assert text == "lasaña"


def test_a_template_name_outranks_another_templates_alias() -> None:
    """The one failure a matcher must never have: printing a different template than was named.

    Without name precedence, an ``aliases: [nevera]`` on the freezer template would resolve — and
    print — in place of the fridge template's own name.
    """
    catalog = [
        {**NO_REQUIRED_TEMPLATE, "name": "nevera"},
        {**NO_REQUIRED_TEMPLATE, "name": "congelador", "aliases": ["nevera"]},
    ]
    template, _text = _split_template_and_text("nevera", catalog)
    assert template is not None
    assert template["name"] == "nevera"


def test_an_alias_two_templates_claim_resolves_to_neither() -> None:
    catalog = [
        {**NO_REQUIRED_TEMPLATE, "name": "nevera", "aliases": ["frio"]},
        {**NO_REQUIRED_TEMPLATE, "name": "congelador", "aliases": ["frio"]},
    ]
    assert _split_template_and_text("frio", catalog) == (None, None)


def test_an_alias_that_normalizes_to_nothing_is_ignored() -> None:
    """``"."`` is a substring of nothing but the empty string is a substring of EVERY name.

    An alias of pure punctuation would otherwise enter the index with an empty key and the
    substring rule would hand back whichever catalog name is longest.
    """
    catalog = [{**NO_REQUIRED_TEMPLATE, "name": "congelador", "aliases": ["..."]}]
    assert _split_template_and_text("...", catalog) == (None, None)


def test_a_missing_aliases_key_is_not_an_error() -> None:
    """The key is absent on a labelito older than the release that added aliases."""
    template, _text = _split_template_and_text(
        "nevera", [{**NO_REQUIRED_TEMPLATE, "name": "nevera"}]
    )
    assert template is not None


@pytest.mark.parametrize("aliases", [None, [], "not-a-list", {"a": 1}, 42])
def test_a_malformed_aliases_value_adds_no_spoken_forms(aliases: Any) -> None:
    """labelito validates aliases, but the integration must not trust a server it does not own.

    An earlier version of this test only asserted that the catalog still matched — which it did,
    while a scalar `aliases` was quietly registering one spoken form per CHARACTER. Not crashing
    was never the property worth pinning; not inventing vocabulary is.
    """
    catalog = [{**NO_REQUIRED_TEMPLATE, "name": "nevera", "aliases": aliases}]
    assert sorted(spoken_name_index(catalog)) == ["nevera"]
    template, _text = _split_template_and_text("nevera", catalog)
    assert template is not None
    assert template["name"] == "nevera"


@pytest.mark.parametrize("spoken", ["nada", "otro", "lista", "aire"])
def test_a_scalar_aliases_value_cannot_print_a_wrong_label(spoken: str) -> None:
    """The consequence a type check prevents, stated as the failure it would be.

    With ``aliases: "not-a-list"`` the index gained the one-letter forms n, o, t, a, l, i, s — and
    a single letter is a substring of almost any utterance, so _fuzzy_match_template's containment
    rule resolved it. Every word below was measured resolving to `nevera` and would have printed
    that label.
    """
    catalog = [
        {**NO_REQUIRED_TEMPLATE, "name": "nevera", "aliases": "not-a-list"},
        {**NO_REQUIRED_TEMPLATE, "name": "congelador"},
    ]
    assert _split_template_and_text(spoken, catalog) == (None, None)


def test_non_string_alias_entries_are_skipped_individually() -> None:
    """One bad entry must not cost the good ones next to it."""
    catalog = [{**NO_REQUIRED_TEMPLATE, "name": "nevera", "aliases": [123, {"a": 1}, None, "frio"]}]
    assert sorted(spoken_name_index(catalog)) == ["frio", "nevera"]


def test_the_generator_vocabulary_is_the_matchers_vocabulary() -> None:
    """Every spoken form offered to the grammar must resolve here to the same template.

    This is the contract between the two layers: the grammar hands back the canonical name, and
    the handler looks it up again. A form that resolved differently — or not at all — would be
    matched by voice and then reported as an unknown template.
    """
    from custom_components.labelito.intents import resolvable_spoken_forms

    # Includes a contested pair on purpose: on a clean catalog every form round-trips trivially
    # and this test proves nothing. "pantry-1"/"pantry_1" both normalize to "pantry 1", so the
    # name is unresolvable while the alias on one of them stays unique — the exact shape that must
    # not reach the grammar.
    catalog = [
        *ALIASED_CATALOG,
        {**NO_REQUIRED_TEMPLATE, "name": "meal-prep"},
        {**NO_REQUIRED_TEMPLATE, "name": "pantry-1", "aliases": ["despensa uno"]},
        {**NO_REQUIRED_TEMPLATE, "name": "pantry_1"},
    ]
    for spoken, name in resolvable_spoken_forms(catalog).items():
        template, _text = _split_template_and_text(spoken, catalog)
        assert template is not None, spoken
        assert template["name"] == name, spoken
        # And the canonical name it returns must itself resolve, since that is what the handler
        # receives from a closed-list match.
        round_trip, _text = _split_template_and_text(name, catalog)
        assert round_trip is not None and round_trip["name"] == name, name


# --- a malformed catalog entry costs only itself ---------------------------------------------
#
# The catalog is an HTTP response from a service this integration does not own. Reading
# template["name"] on a malformed entry raises KeyError, AttributeError or TypeError depending on
# the shape — and before the entries were vetted, one bad one raised out of the shared index and
# broke every spoken match for the whole catalog.

MALFORMED_ENTRIES: list[Any] = [
    {"description": "no name at all"},
    {"name": 42},
    {"name": None},
    {"name": ""},
    "not-a-mapping",
    123,
    None,
]


@pytest.mark.parametrize("bad", MALFORMED_ENTRIES)
def test_a_malformed_catalog_entry_does_not_break_its_neighbours(bad: Any) -> None:
    catalog = [bad, {**NO_REQUIRED_TEMPLATE, "name": "nevera"}]
    assert sorted(spoken_name_index(catalog)) == ["nevera"]

    template, _text = _split_template_and_text("nevera", catalog)
    assert template is not None
    assert template["name"] == "nevera"


@pytest.mark.parametrize("bad", MALFORMED_ENTRIES)
def test_a_malformed_catalog_entry_is_not_reachable_by_voice(bad: Any) -> None:
    """It contributes no spoken form, which is not a loss: an unsayable name cannot be said."""
    assert spoken_name_index([bad]) == {}


def test_aliases_on_a_malformed_entry_are_ignored_too() -> None:
    """An entry with no usable name cannot lend its aliases to anything.

    ``out`` has to be a canonical name, so an alias whose template has none could only produce a
    match the handler would then report as unknown.
    """
    assert spoken_name_index([{"name": 42, "aliases": ["frio"]}]) == {}


@pytest.mark.parametrize("name", ["...", "   ", "-", "_", "¿?", "«»"])
def test_a_punctuation_only_slot_cannot_resolve_a_template(name: str) -> None:
    """A template whose name normalizes to nothing must not be reachable by an empty spoken form.

    _split_template_and_text tests exact membership before _fuzzy_match_template's normalized-empty
    guard runs, so an empty key in the index was matched directly — a speech-to-text engine that
    heard only punctuation printed that template. Measured before the fix: "..." resolved to it.
    """
    catalog = [
        {**NO_REQUIRED_TEMPLATE, "name": name},
        {**NO_REQUIRED_TEMPLATE, "name": "nevera"},
    ]
    assert sorted(spoken_name_index(catalog)) == ["nevera"]
    assert _split_template_and_text("...", catalog) == (None, None)
    assert _split_template_and_text(name, catalog) == (None, None)


def test_unsayable_names_are_reported_so_the_rename_is_discoverable() -> None:
    from custom_components.labelito.intents import unsayable_template_names

    catalog = [
        {**NO_REQUIRED_TEMPLATE, "name": "..."},
        {**NO_REQUIRED_TEMPLATE, "name": "   "},
        {**NO_REQUIRED_TEMPLATE, "name": "nevera"},
        {"name": 42},  # no usable name at all: a shape problem, not a pronunciation one
    ]
    # Sorted, like every other report the generator produces: deterministic output is what
    # lets an unchanged catalog render a byte-identical file.
    assert unsayable_template_names(catalog) == ["   ", "..."]
