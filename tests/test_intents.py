"""Tests for the LabelitoPrint Assist intent handler.

The regression these lock down: HA's ``recognize_best`` folds the whole Spanish utterance into the
greedy trailing ``{template}`` wildcard (see docs/voice-assist.md), so the handler receives
``template="pantry para sopa de tomate"`` and no ``text`` slot. The handler must recover the free
text, map it to the template's first required field, and — when no text can be recovered for a
required-field template — speak an actionable error instead of forwarding a doomed print.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import intent

from custom_components.labelito.const import INTENT_PRINT
from custom_components.labelito.intents import (
    LabelitoPrintIntentHandler,
    _split_template_and_text,
)

from .const import MOCK_TEMPLATES

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


def _make_coordinator(templates: list[dict[str, Any]] | None = None) -> Mock:
    catalog = list(templates if templates is not None else MOCK_TEMPLATES)
    coordinator = Mock()
    coordinator.async_get_templates = AsyncMock(return_value=catalog)
    coordinator.template_names = Mock(return_value=[t["name"] for t in catalog])
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
) -> tuple[intent.IntentResponse, AsyncMock]:
    coordinator = coordinator or _make_coordinator()
    handler = LabelitoPrintIntentHandler()
    with (
        patch(
            "custom_components.labelito.intents.resolve_coordinator",
            return_value=coordinator,
        ),
        patch(
            "custom_components.labelito.intents.async_execute_print",
            new=AsyncMock(),
        ) as execute,
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


async def test_no_text_on_required_template_speaks_needs_text(hass: HomeAssistant) -> None:
    """pantry requires 'title'; with nothing to fill, warn instead of forwarding a 422."""
    response, execute = await _handle(hass, {"template": "pantry"})
    execute.assert_not_awaited()
    assert _speech(response) == "Necesito el texto para la etiqueta de pantry."


async def test_no_text_on_required_template_english(hass: HomeAssistant) -> None:
    response, execute = await _handle(hass, {"template": "pantry"}, language="en")
    execute.assert_not_awaited()
    assert _speech(response) == "I need the text to put on the pantry label."


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


# --- the split helper in isolation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("spoken", "expected_name", "expected_text"),
    [
        ("pantry para sopa de tomate", "pantry", "sopa de tomate"),
        ("pantry que diga sopa de tomate", "pantry", "sopa de tomate"),
        ("pantry", "pantry", None),
        ("banana", None, None),
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


@pytest.mark.parametrize(
    ("spoken", "templates", "expected_name", "expected_text"),
    [
        # Exact multi-word template must win over a shorter prefix — no bogus split into text.
        ("freezer dated", _OVERLAP_CATALOG, "freezer-dated", None),
        # ...but a real overcapture on the multi-word template still recovers the text.
        ("freezer dated para lasagna", _OVERLAP_CATALOG, "freezer-dated", "lasagna"),
        # Only ONE connector phrase is stripped: text that begins with a connector word survives.
        ("pantry para para mañana", [{"name": "pantry"}], "pantry", "para mañana"),
        # A trailing word that is not a connector is not mistaken for text (no split without one).
        ("freezer lasagna", [{"name": "freezer"}], "freezer", None),
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
        data = yaml.safe_load((repo / "sentences" / lang / "labelito.yaml").read_text())
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
