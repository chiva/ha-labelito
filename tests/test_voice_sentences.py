"""Tests for the generated closed-list voice grammar.

Three claims carry this feature, and each is checked against the real thing rather than asserted
in prose:

1. **The closed list wins when it can, and the wildcard catches everything else.** Checked by
   feeding the actual generated YAML through ``hassil.recognize_best`` with the same arguments
   Home Assistant's default agent passes.
2. **Either file is a valid grammar on its own.** hassil raises ``MissingListError`` at
   *recognition* time for a sentence referencing an undefined list, which would take down every
   result for that language — including the wildcard sentences that are supposed to be the safety
   net. This is the reason the closed-list sentences and their list ship in one file.
3. **Nothing is written into the config directory unbidden, and nothing is rewritten for free.**
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.labelito.const import DOMAIN, INTENT_PRINT, SERVICE_WRITE_VOICE_SENTENCES
from custom_components.labelito.voice_sentences import (
    _CATALOG_TO_DISK,
    CLOSED_LIST_FILENAME,
    CLOSED_LIST_SLOT,
    CUSTOM_SENTENCES_DIR,
    GENERATED_MARKER,
    SUPPORTED_LANGUAGES,
    TEMPLATE_LIST,
    WILDCARD_FILENAME,
    WILDCARD_SLOT,
    WILDCARD_TEMPLATE_SLOT,
    SentencesResult,
    _closed_list_values,
    async_refresh_voice_sentences,
    async_sync_voice_sentences,
    async_write_voice_sentences,
    closed_list_document,
    wildcard_document,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
METADATA_CUSTOM_SENTENCE = "hass_custom_sentence"


@pytest.fixture(autouse=True)
def isolated_config_dir(hass: HomeAssistant, tmp_path: Path) -> None:
    """Point Home Assistant's config directory at a per-test temporary one.

    Not a nicety: pytest-homeassistant-custom-component's default config directory lives INSIDE the
    installed package, so without this every write would land in one shared directory — tests would
    see each other's files (an "is it absent?" assertion passing only when it runs first) and the
    suite would leave files behind in site-packages.
    """
    hass.config.config_dir = str(tmp_path)


def _template(name: str, aliases: list[str] | None = None) -> dict[str, Any]:
    template: dict[str, Any] = {
        "name": name,
        "description": name,
        "label": "62",
        "rotate": 0,
        "fields": {"required": ["title"], "optional": []},
        "media": None,
        "uses_seq": False,
    }
    if aliases is not None:
        template["aliases"] = aliases
    return template


def _values(templates: list[dict[str, Any]]) -> dict[str, str]:
    values, _unspeakable = _closed_list_values(templates)
    return {value["in"]: value["out"] for value in values}


# --- the vocabulary ---------------------------------------------------------------------------


def test_names_are_despaced_into_spoken_forms() -> None:
    """A saved name is never said aloud with its separators; the spoken form is what goes in."""
    assert _values([_template("meal-prep"), _template("simple_text")]) == {
        "meal prep": "meal-prep",
        "simple text": "simple_text",
    }


def test_aliases_widen_the_vocabulary_and_still_print_the_canonical_name() -> None:
    """``out`` is always the saved name — an alias changes what is heard, never what is printed."""
    assert _values([_template("congelador", ["congelado", "Congelación"])]) == {
        "congelador": "congelador",
        "congelado": "congelador",
        "congelación": "congelador",
    }


def test_a_template_without_aliases_needs_no_aliases_key() -> None:
    """The key is absent on a labelito older than the release that added it."""
    assert _values([_template("pantry")]) == {"pantry": "pantry"}


def test_names_outrank_aliases() -> None:
    """An alias must never resolve in place of another template's actual name.

    Without this precedence, adding ``aliases: [nevera]`` to a freezer template would hijack the
    fridge template's own name and print the wrong label — the one failure a matcher must not have.
    """
    values = _values([_template("nevera"), _template("congelador", ["nevera"])])
    assert values["nevera"] == "nevera"


def test_a_form_two_templates_claim_is_dropped() -> None:
    """Ambiguity resolves to nothing, so the handler can speak the real catalog instead."""
    values, _unspeakable = _closed_list_values(
        [_template("pantry-1"), _template("pantry_1"), _template("nevera")]
    )
    assert {value["in"] for value in values} == {"nevera"}


def test_an_alias_two_templates_claim_is_dropped() -> None:
    values = _values([_template("nevera", ["frio"]), _template("congelador", ["frio"])])
    assert "frio" not in values
    assert values == {"nevera": "nevera", "congelador": "congelador"}


def test_an_alias_of_a_template_with_a_contested_name_is_dropped() -> None:
    """The subtle one: a unique alias on a template whose own NAME is ambiguous.

    The grammar hands the handler the canonical name, which the handler resolves again — and it has
    deliberately stopped resolving a contested name. Emitting the alias would produce a match the
    handler then reports as an unknown template.
    """
    values = _values(
        [_template("pantry-1", ["despensa uno"]), _template("pantry_1"), _template("nevera")]
    )
    assert values == {"nevera": "nevera"}


def test_a_name_carrying_grammar_syntax_is_left_out_and_reported() -> None:
    """labelito constrains the names IT saves, but a hand-placed YAML file may be named anything.

    hassil would parse ``(`` as a group rather than match it, so the name is left to the wildcard
    sentences and named in the response so the user can rename it.
    """
    values, unspeakable = _closed_list_values([_template("weird (name)"), _template("nevera")])
    assert {value["in"] for value in values} == {"nevera"}
    assert unspeakable == ["weird (name)"]


def test_values_are_sorted_so_an_unchanged_catalog_renders_identically() -> None:
    """Deterministic output is what lets the writer skip a rewrite (and a conversation reload)."""
    templates = [_template("nevera"), _template("congelador"), _template("despensa")]
    first, _ = _closed_list_values(templates)
    second, _ = _closed_list_values(list(reversed(templates)))
    assert first == second == sorted(first, key=lambda value: value["in"])


# --- the documents ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_the_shipped_wildcard_file_matches_the_generated_one(language: str) -> None:
    """The repo's custom_sentences/ copy and the integration's constants must not drift.

    They exist separately because HACS installs ``custom_components/`` only, so the integration
    cannot read the repo folder at runtime — and a user who installed the file by hand must end up
    with the same grammar as one who let the service write it.
    """
    shipped = yaml.safe_load(
        (REPO_ROOT / CUSTOM_SENTENCES_DIR / language / WILDCARD_FILENAME).read_text(
            encoding="utf-8"
        )
    )
    assert shipped == wildcard_document(language)


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_both_grammars_use_the_same_sentences(language: str) -> None:
    """Only the slot differs, so the two can never come to accept different phrasings."""
    closed = closed_list_document(language, [{"in": "x", "out": "x"}])
    wildcard = wildcard_document(language)

    def _without_template_slot(document: dict[str, Any], slot: str) -> list[str]:
        return [
            sentence.replace(slot, WILDCARD_SLOT)
            for group in document["intents"][INTENT_PRINT]["data"]
            for sentence in group["sentences"]
        ]

    assert _without_template_slot(closed, CLOSED_LIST_SLOT) == _without_template_slot(
        wildcard, WILDCARD_TEMPLATE_SLOT
    )


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_each_document_defines_every_list_its_sentences_reference(language: str) -> None:
    """The invariant behind the two-file split: either file alone must be a complete grammar.

    Checked structurally here and behaviourally in the hassil tests below.
    """
    for document in (wildcard_document(language), closed_list_document(language, [])):
        referenced = {
            fragment.split("}")[0].split(":")[0]
            for group in document["intents"][INTENT_PRINT]["data"]
            for sentence in group["sentences"]
            for fragment in sentence.split("{")[1:]
        }
        assert referenced <= set(document["lists"]), (
            f"{language}: {referenced - set(document['lists'])} referenced but not defined"
        )


# --- writing ---------------------------------------------------------------------------------


def _path(hass: HomeAssistant, language: str, filename: str) -> Path:
    return Path(hass.config.path(CUSTOM_SENTENCES_DIR, language, filename))


async def test_create_writes_both_files_for_every_language(hass: HomeAssistant) -> None:
    result = await async_write_voice_sentences(hass, [_template("nevera")], create=True)

    for language in SUPPORTED_LANGUAGES:
        assert _path(hass, language, WILDCARD_FILENAME).exists()
        assert _path(hass, language, CLOSED_LIST_FILENAME).exists()
    assert len(result.written) == 2 * len(SUPPORTED_LANGUAGES)
    assert result.spoken_forms == 1
    # Reported relative to the config directory, so a response never leaks the host's layout.
    assert all(not Path(path).is_absolute() for path in result.written)


async def test_create_can_be_narrowed_to_one_language(hass: HomeAssistant) -> None:
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    assert _path(hass, "es", CLOSED_LIST_FILENAME).exists()
    assert not _path(hass, "en", CLOSED_LIST_FILENAME).exists()


async def test_without_create_nothing_is_written(hass: HomeAssistant) -> None:
    """Setting up a printer must not put files in the config directory.

    This is the whole opt-in mechanism: the automatic refresh runs with create=False, so until the
    service has been called once there is nothing to keep in step and nothing appears.
    """
    result = await async_write_voice_sentences(hass, [_template("nevera")], create=False)
    assert result.written == []
    assert not _path(hass, "es", CLOSED_LIST_FILENAME).exists()
    assert not _path(hass, "es", WILDCARD_FILENAME).exists()


async def test_without_create_an_existing_generated_file_is_updated(hass: HomeAssistant) -> None:
    """Having opted in, the list follows the catalog on its own."""
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    result = await async_write_voice_sentences(
        hass, [_template("nevera"), _template("congelador")], ["es"], create=False
    )

    assert result.written == [f"{CUSTOM_SENTENCES_DIR}/es/{CLOSED_LIST_FILENAME}"]
    document = yaml.safe_load(_path(hass, "es", CLOSED_LIST_FILENAME).read_text(encoding="utf-8"))
    assert {value["out"] for value in document["lists"][TEMPLATE_LIST]["values"]} == {
        "nevera",
        "congelador",
    }


async def test_an_unchanged_catalog_rewrites_nothing(hass: HomeAssistant) -> None:
    """No write means no conversation reload — a poll every few minutes must be free."""
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    result = await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=False)

    assert result.written == []
    assert result.unchanged == [f"{CUSTOM_SENTENCES_DIR}/es/{CLOSED_LIST_FILENAME}"]
    assert result.reloaded is False


async def test_the_users_wildcard_file_is_never_overwritten(hass: HomeAssistant) -> None:
    """It is written once if missing and is the user's to edit from then on."""
    path = _path(hass, "es", WILDCARD_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# mine\n", encoding="utf-8")

    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    assert path.read_text(encoding="utf-8") == "# mine\n"


async def test_an_empty_catalog_still_writes_the_generated_file(hass: HomeAssistant) -> None:
    """An empty list is written, not skipped — the file's existence IS the opt-in record.

    An earlier version skipped it, on the theory that a list matching nothing is a grammar that
    can only fail. Both halves were wrong: hassil parses `values: []` fine and its sentences
    simply never match (see test_an_empty_closed_list_is_harmless), and skipping the file meant
    the opt-in was never recorded.
    """
    await async_write_voice_sentences(hass, [], ["es"], create=True)
    path = _path(hass, "es", CLOSED_LIST_FILENAME)
    assert _path(hass, "es", WILDCARD_FILENAME).exists()
    assert path.exists()
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["lists"][TEMPLATE_LIST]["values"] == []


async def test_an_empty_catalog_does_not_lose_the_opt_in(hass: HomeAssistant) -> None:
    """Opting in while the catalog is empty must still leave the refresh able to work later.

    The regression: with the generated file skipped on an empty catalog, every later
    ``create=False`` refresh saw no file, concluded the user had never opted in, and wrote nothing
    — so templates added afterwards silently never got an exact grammar, with no way to tell
    except running the service by hand again.
    """
    await async_write_voice_sentences(hass, [], ["es"], create=True)

    result = await async_write_voice_sentences(
        hass, [_template("nevera"), _template("congelador")], ["es"], create=False
    )

    assert result.written == [f"{CUSTOM_SENTENCES_DIR}/es/{CLOSED_LIST_FILENAME}"]
    document = yaml.safe_load(_path(hass, "es", CLOSED_LIST_FILENAME).read_text(encoding="utf-8"))
    assert {value["out"] for value in document["lists"][TEMPLATE_LIST]["values"]} == {
        "nevera",
        "congelador",
    }


async def test_without_opt_in_the_catalog_is_never_touched(hass: HomeAssistant) -> None:
    """A refresh for a language nobody opted into does no work at all, not just no writing.

    Asserted as "the derivation was not called", because "nothing was written" is true either way
    — an earlier version of this test checked only the result and passed with the check removed.
    What the ordering buys: every status poll would otherwise run the whole catalog through the
    generator for nothing, and at startup run it where an exception aborts the config entry, for a
    user who never enabled generated sentences at all.
    """
    with patch(
        "custom_components.labelito.voice_sentences._closed_list_values"
    ) as derive_vocabulary:
        result = await async_write_voice_sentences(
            hass, [_template("nevera")], ["es"], create=False
        )

    derive_vocabulary.assert_not_called()
    assert result == SentencesResult()
    assert not _path(hass, "es", CLOSED_LIST_FILENAME).exists()


async def test_writing_reloads_the_conversation_integration(hass: HomeAssistant) -> None:
    """Without the reload a regenerated file only takes effect on the next restart."""
    calls: list[Any] = []
    hass.services.async_register("conversation", "reload", lambda call: calls.append(call))

    result = await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    assert result.reloaded is True
    assert len(calls) == 1


async def test_no_conversation_integration_is_not_an_error(hass: HomeAssistant) -> None:
    """Voice is optional: writing the files must work on an install with no conversation agent."""
    result = await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    assert result.written
    assert result.reloaded is False


# --- failure isolation ------------------------------------------------------------------------


async def test_a_failed_write_leaves_the_previous_file_intact(hass: HomeAssistant) -> None:
    """A partial rewrite of a live sentence file is worse than no rewrite at all.

    A truncated closed-list file can still parse as a mapping whose sentences reference the list
    its cut-off tail was supposed to define — the MissingListError case that discards EVERY result
    for the language. Since the rewrite also runs from the automatic refresh, nobody would be
    watching when it happened, so the old file has to survive the failure.
    """
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    path = _path(hass, "es", CLOSED_LIST_FILENAME)
    before = path.read_text(encoding="utf-8")

    with (
        patch.object(Path, "replace", side_effect=OSError("no space left on device")),
        pytest.raises(OSError, match="no space left"),
    ):
        await async_write_voice_sentences(
            hass, [_template("nevera"), _template("congelador")], ["es"], create=False
        )

    assert path.read_text(encoding="utf-8") == before


async def test_a_failed_write_leaves_no_yaml_home_assistant_would_load(
    hass: HomeAssistant,
) -> None:
    """The staged file must be invisible to Home Assistant's ``*.yaml`` scan, and then cleaned up.

    Staging as `<name>.yaml.something` would put a half-written grammar in the very directory the
    conversation agent globs — trading a truncation window for a permanent broken file.
    """
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    directory = _path(hass, "es", CLOSED_LIST_FILENAME).parent

    with (
        patch.object(Path, "replace", side_effect=OSError("boom")),
        pytest.raises(OSError, match="boom"),
    ):
        await async_write_voice_sentences(hass, [_template("otra")], ["es"], create=False)

    assert sorted(item.name for item in directory.glob("*.yaml")) == [
        CLOSED_LIST_FILENAME,
        WILDCARD_FILENAME,
    ]
    # And nothing left behind under any name.
    assert sorted(item.name for item in directory.iterdir()) == [
        CLOSED_LIST_FILENAME,
        WILDCARD_FILENAME,
    ]


async def test_an_unreadable_generated_file_is_replaced(hass: HomeAssistant) -> None:
    """Comparing against a corrupted cache file must not be fatal.

    ``read_text`` raises UnicodeDecodeError on invalid UTF-8 — a ValueError, NOT an OSError — so
    before it was caught it escaped both callers' error handling and, at startup, took the config
    entry down over a file we were about to overwrite anyway.
    """
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    path = _path(hass, "es", CLOSED_LIST_FILENAME)
    # Keeps the marker: ownership is decided on the file's first BYTES precisely so a file that
    # is ours but undecodable is still recognized. Garbage with no marker is a conflict, not a
    # replacement — covered separately.
    path.write_bytes(GENERATED_MARKER.encode() + b"\n\xff\xfe not valid utf-8")

    result = await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=False)

    assert result.written == [f"{CUSTOM_SENTENCES_DIR}/es/{CLOSED_LIST_FILENAME}"]
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["language"] == "es"


async def test_a_corrupted_user_file_is_left_alone(hass: HomeAssistant) -> None:
    """The write-if-absent rule holds even when the file is unreadable — it is still not ours."""
    path = _path(hass, "es", WILDCARD_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xff\xfe mine, broken")

    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    assert path.read_bytes() == b"\xff\xfe mine, broken"


async def test_a_reload_failure_is_reported_not_raised(hass: HomeAssistant) -> None:
    """A conversation problem must never stop labelito from working.

    The reload is a convenience called after the files are already on disk, and its fallback is
    the restart the user would otherwise have needed. Raising would let an invalid custom-sentence
    file somebody hand-edited abort the printer integration's setup instead.
    """

    def _boom(call: Any) -> None:
        raise HomeAssistantError("invalid custom sentences somewhere else")

    hass.services.async_register("conversation", "reload", _boom)

    result = await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)

    assert result.reloaded is False
    assert len(result.written) == 2
    assert _path(hass, "es", CLOSED_LIST_FILENAME).exists()


async def test_the_automatic_refresh_never_raises(hass: HomeAssistant) -> None:
    """The isolation contract, tested directly rather than through a scenario.

    The realistic trigger — a schema-drifted catalog — is now also handled by vetting entries, so a
    scenario test passes whether or not this isolation exists. What has to hold regardless is the
    rule itself: the automatic refresh drives file writes and a call into another integration, over
    data from a service this one does not own, so the set of exceptions it can produce is not
    enumerable here and none of them are a reason to stop printing labels.
    """
    # The opt-in has to exist, or the refresh returns before it reaches anything that could
    # raise — an earlier version of this test skipped straight past the code it was testing.
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)

    coordinator = Mock()
    coordinator.hass = hass
    coordinator.async_get_templates = AsyncMock(return_value=[_template("nevera")])

    with patch(
        "custom_components.labelito.voice_sentences._closed_list_values",
        side_effect=ValueError("something nobody predicted"),
    ) as derive_vocabulary:
        await async_refresh_voice_sentences(coordinator)

    derive_vocabulary.assert_called_once()


async def test_the_catalog_snapshot_is_taken_under_the_lock(hass: HomeAssistant) -> None:
    """The invariant: reading the catalog and committing it are ONE serialized unit.

    Both paths await between snapshot and commit, which is all the event loop needs to interleave
    them — the refresh snapshots catalog A, the service force-fetches B and writes it, and the
    refresh then commits A on top, atomically, so B is gone. Self-correcting only on the next
    refresh, up to TEMPLATE_CACHE_TTL (15 minutes) later, right after a user ran the service to
    bring the list up to date.

    Asserted where it has to hold rather than by staging the interleaving: with the lock spanning
    the fetch, the two sequences CANNOT interleave, so any test that reproduced the old ordering
    would deadlock instead of failing. (That is what happened when this was first written.)
    """
    await async_write_voice_sentences(hass, [_template("old")], ["es"], create=True)

    coordinator = Mock()
    coordinator.hass = hass
    seen_locked: list[bool] = []

    async def _fetch(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        seen_locked.append(_CATALOG_TO_DISK.locked())
        return [_template("old")]

    coordinator.async_get_templates = AsyncMock(side_effect=_fetch)
    await async_sync_voice_sentences(coordinator, ["es"], create=False, force_refresh=False)

    assert seen_locked == [True], "the catalog was read outside the lock that guards the write"


async def test_two_syncs_cannot_run_at_once(hass: HomeAssistant) -> None:
    """Mutual exclusion, driven without either call depending on the other.

    The first sync blocks inside its fetch on an event the TEST releases. While it is blocked, a
    second sync is started: its fetch must not have run, because the lock is still held. That is
    the ordering guarantee the interleaving needed in order to exist.
    """
    await async_write_voice_sentences(hass, [_template("old")], ["es"], create=True)

    release = asyncio.Event()
    first_fetching = asyncio.Event()

    slow = Mock()
    slow.hass = hass

    async def _slow_fetch(*_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        first_fetching.set()
        await release.wait()
        return [_template("old")]

    slow.async_get_templates = AsyncMock(side_effect=_slow_fetch)

    fast = Mock()
    fast.hass = hass
    fast.async_get_templates = AsyncMock(return_value=[_template("old"), _template("new")])

    first = asyncio.create_task(
        async_sync_voice_sentences(slow, ["es"], create=False, force_refresh=False)
    )
    await asyncio.wait_for(first_fetching.wait(), timeout=5)

    second = asyncio.create_task(
        async_sync_voice_sentences(fast, ["es"], create=False, force_refresh=True)
    )
    await asyncio.sleep(0)
    fast.async_get_templates.assert_not_awaited()  # still waiting for the lock

    release.set()
    await asyncio.wait_for(asyncio.gather(first, second), timeout=5)
    fast.async_get_templates.assert_awaited_once()

    # The second sync ran last, so its newer catalog is what is on disk.
    document = yaml.safe_load(_path(hass, "es", CLOSED_LIST_FILENAME).read_text(encoding="utf-8"))
    assert {value["out"] for value in document["lists"][TEMPLATE_LIST]["values"]} == {"old", "new"}


async def test_the_service_still_reports_a_write_failure(hass: HomeAssistant) -> None:
    """The asymmetry that makes the isolation acceptable: a user who asked still gets the error."""
    from custom_components.labelito.voice_sentences import async_setup_voice_sentences_service

    coordinator = Mock()
    coordinator.hass = hass
    coordinator.async_get_templates = AsyncMock(return_value=[_template("nevera")])
    async_setup_voice_sentences_service(hass)

    with (
        patch.object(Path, "replace", side_effect=OSError("read-only file system")),
        patch(
            "custom_components.labelito.voice_sentences.resolve_coordinator",
            return_value=coordinator,
        ),
        pytest.raises(HomeAssistantError, match="Could not write the voice sentence files"),
    ):
        await hass.services.async_call(
            DOMAIN, SERVICE_WRITE_VOICE_SENTENCES, {"language": ["es"]}, blocking=True
        )


# --- ownership: only files this integration wrote are rewritten -------------------------------


async def test_a_file_we_did_not_write_is_never_overwritten(hass: HomeAssistant) -> None:
    """Silent config loss, and the automatic path made it worse than a service call would.

    Treating any file with the generated NAME as ours meant a hand-written grammar called
    labelito-templates.yaml was replaced — at startup, from the status poll, with no service call
    involved, and the reload made the replacement active immediately.
    """
    path = _path(hass, "es", CLOSED_LIST_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    mine = "# my own labelito sentences\nlanguage: es\n"
    path.write_text(mine, encoding="utf-8")

    result = await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)

    assert path.read_text(encoding="utf-8") == mine
    assert result.conflicts == [f"{CUSTOM_SENTENCES_DIR}/es/{CLOSED_LIST_FILENAME}"]
    assert result.written == [f"{CUSTOM_SENTENCES_DIR}/es/{WILDCARD_FILENAME}"]


async def test_an_unowned_file_is_not_an_opt_in(hass: HomeAssistant) -> None:
    """Ownership, not mere existence, is what the automatic refresh keys off.

    Otherwise the refresh would find a file it may not touch, and — because existence doubles as
    the opt-in record — would keep finding it forever while doing nothing.
    """
    path = _path(hass, "es", CLOSED_LIST_FILENAME)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# not mine\n", encoding="utf-8")

    with patch(
        "custom_components.labelito.voice_sentences._closed_list_values"
    ) as derive_vocabulary:
        result = await async_write_voice_sentences(
            hass, [_template("nevera")], ["es"], create=False
        )

    derive_vocabulary.assert_not_called()
    assert result == SentencesResult()
    assert path.read_text(encoding="utf-8") == "# not mine\n"


async def test_a_generated_file_carries_the_marker_it_is_recognized_by(
    hass: HomeAssistant,
) -> None:
    """The marker is load-bearing, so its presence is pinned rather than assumed.

    If the header were reworded so the file no longer started with it, every file already on disk
    would become unrecognized — the refresh would report a conflict forever and silently stop
    updating.
    """
    await async_write_voice_sentences(hass, [_template("nevera")], ["es"], create=True)
    content = _path(hass, "es", CLOSED_LIST_FILENAME).read_text(encoding="utf-8")
    assert content.startswith(GENERATED_MARKER)


# --- namespaced lists -------------------------------------------------------------------------


@pytest.mark.parametrize("language", SUPPORTED_LANGUAGES)
def test_every_list_is_namespaced(language: str) -> None:
    """`lists` is one namespace across every custom-sentence file Home Assistant merges."""
    for document in (wildcard_document(language), closed_list_document(language, [])):
        for name in document["lists"]:
            assert name.startswith(f"{DOMAIN}_"), name


def test_an_unrelated_grammar_with_generic_list_names_does_not_interfere() -> None:
    """The collision, measured before and prevented after.

    With a generic `template_name`, another file's values merged into ours and "imprime una
    etiqueta de tele" resolved to template="television" — a value from a grammar that has nothing
    to do with labels, which the handler would then report as an unknown template (or, if a real
    template happened to share the name, print).
    """
    someone_else = {
        "language": "es",
        "intents": {
            "SomeoneElse": {"data": [{"sentences": ["pon la {template_name} en la {text}"]}]}
        },
        "lists": {
            "template_name": {"values": [{"in": "tele", "out": "television"}]},
            "text": {"values": [{"in": "cocina", "out": "kitchen"}]},
        },
    }
    values, _unspeakable = _closed_list_values([_template("congelador")])
    documents = [wildcard_document("es"), closed_list_document("es", values), someone_else]

    # Their value cannot reach our slot...
    result = _recognize(documents, "imprime una etiqueta de tele")
    assert result is not None
    assert _slots(result) == {"template": "tele"}  # the wildcard, not their `television`

    # ...ours still resolves exactly, and theirs is untouched.
    result = _recognize(documents, "imprime una etiqueta de congelador para lasaña")
    assert result is not None
    assert _slots(result) == {"template": "congelador", "text": "lasaña"}
    result = _recognize(documents, "pon la tele en la cocina")
    assert result is not None
    assert result.intent.name == "SomeoneElse"


# --- names that cannot be spoken --------------------------------------------------------------


@pytest.mark.parametrize("name", ["...", "   ", "-", "_", "¿?"])
def test_a_name_that_normalizes_to_nothing_is_excluded_and_reported(name: str) -> None:
    """An empty spoken form is catastrophic in the index, not merely useless.

    _split_template_and_text tests exact membership first, so a punctuation-only slot from a
    speech-to-text engine matched the empty key and printed that template; the generator also
    emitted `in: ""` into the closed list. Aliases were already guarded; names were not.
    """
    values, unspeakable = _closed_list_values([_template(name), _template("nevera")])
    assert [value["in"] for value in values] == ["nevera"]
    assert unspeakable == [name]


# --- untrusted catalog data -------------------------------------------------------------------


@pytest.mark.parametrize("aliases", ["not-a-list", {"a": 1}, 42])
def test_a_malformed_aliases_container_contributes_nothing(aliases: Any) -> None:
    """A scalar `aliases` iterates per CHARACTER, and one-letter forms match almost anything."""
    template = _template("nevera")
    template["aliases"] = aliases
    values, _unspeakable = _closed_list_values([template])
    assert [value["in"] for value in values] == ["nevera"]


@pytest.mark.parametrize("alias", ["frio (mucho)", "frio|calor", "frio {name}", "frio [mucho]"])
def test_an_alias_carrying_grammar_syntax_is_excluded(alias: str) -> None:
    """The exclusion has to be on the SPOKEN form, which is what lands in a list value's `in:`.

    Checking the template *name* instead let these through, because the name is perfectly fine —
    the alias is the problem. Measured against hassil 3.12: "frio (mucho)" silently matches "frio
    mucho", "frio|calor" becomes two alternatives, and "frio {name}" raises MissingListError,
    which discards every result for the language.
    """
    values, unspeakable = _closed_list_values([_template("nevera", [alias])])
    assert [value["in"] for value in values] == ["nevera"]
    assert unspeakable == [alias]


def test_an_alias_with_grammar_syntax_does_not_break_recognition() -> None:
    """The end-to-end form of the above: the grammar recognizes rather than raising.

    The utterance matters. A bad list value is a LATENT mine, not an outage: it only raises once
    an utterance walks far enough into the value trie to evaluate it. Measured with the excluded
    value put back, "…de nevera para queso" resolves fine while "…de frio para queso" raises
    MissingListError — so a test using the first utterance passes either way and proves nothing.
    That selective failure is also why the exclusion matters: a mine that fires on some phrasings
    and not others is harder to diagnose than a grammar that never works.
    """
    catalog = [_template("nevera", ["frio {name}"]), _template("congelador")]
    values, _unspeakable = _closed_list_values(catalog)
    documents = [wildcard_document("es"), closed_list_document("es", values)]

    result = _recognize(documents, "haz una etiqueta de frio para queso")
    assert result is not None
    # `frio` is not a usable spoken form, so this falls to the wildcard sentences and the handler
    # takes it from there — the designed degradation, not an error.
    assert _slots(result) == {"template": "frio para queso"}

    # And the names around it still resolve exactly.
    result = _recognize(documents, "haz una etiqueta de nevera para queso")
    assert result is not None
    assert _slots(result) == {"template": "nevera", "text": "queso"}


# --- the service -----------------------------------------------------------------------------


async def test_service_reports_what_it_did(hass: HomeAssistant) -> None:
    coordinator = Mock()
    coordinator.hass = hass
    coordinator.async_get_templates = AsyncMock(
        return_value=[
            _template("nevera"),
            _template("congelador", ["congelado"]),
            _template("pantry-1"),
            _template("pantry_1"),
            _template("weird (name)"),
        ]
    )
    from custom_components.labelito.voice_sentences import async_setup_voice_sentences_service

    async_setup_voice_sentences_service(hass)
    with patch(
        "custom_components.labelito.voice_sentences.resolve_coordinator",
        return_value=coordinator,
    ):
        response = await hass.services.async_call(
            DOMAIN,
            SERVICE_WRITE_VOICE_SENTENCES,
            {"language": ["es"]},
            blocking=True,
            return_response=True,
        )

    assert response is not None
    assert response["spoken_forms"] == 3  # nevera, congelador, congelado
    assert response["ambiguous"] == ["pantry 1"]
    assert response["unspeakable"] == ["weird (name)"]
    # Forced refresh: the service is called right after adding a template, which is exactly when a
    # cached catalog is the wrong answer.
    coordinator.async_get_templates.assert_awaited_once_with(force_refresh=True)


# --- hassil: the claims that actually carry the design ----------------------------------------


def _recognize(documents: list[dict[str, Any]], text: str) -> Any:
    """Recognize ``text`` against merged custom-sentence documents, exactly as HA's agent does."""
    hassil = pytest.importorskip("hassil")
    from hassil.recognize import recognize_best

    merged: dict[str, Any] = {}
    for document in documents:
        # HA stamps every custom-sentence group with this metadata before merging the files, and
        # passes the key to recognize_best to prefer custom sentences over built-in ones.
        stamped = yaml.safe_load(yaml.safe_dump(document))
        for intent_dict in stamped["intents"].values():
            for intent_data in intent_dict["data"]:
                intent_data.setdefault("metadata", {})[METADATA_CUSTOM_SENTENCE] = True
        hassil.merge_dict(merged, stamped)

    return recognize_best(
        text,
        hassil.Intents.from_dict(merged),
        best_metadata_key=METADATA_CUSTOM_SENTENCE,
        best_slot_name="name",
        language="es",
    )


def _slots(result: Any) -> dict[str, Any]:
    return {name: entity.value for name, entity in result.entities.items()}


CATALOG = [
    _template("congelador"),
    _template("nevera"),
    _template("meal-prep", ["comida preparada"]),
    _template("hoy"),
    # A name containing a connector word, alongside the shorter name it could be confused with.
    # docs/voice-assist.md lists this as a limitation of the wildcard grammar; the cases below are
    # where the closed list does and does not change the answer.
    _template("regalo"),
    _template("regalo-para-navidad"),
]


def _both_documents() -> list[dict[str, Any]]:
    values, _ = _closed_list_values(CATALOG)
    return [wildcard_document("es"), closed_list_document("es", values)]


@pytest.mark.parametrize(
    ("utterance", "expected"),
    [
        # The regression the whole closed list exists for: Spanish has no literal after
        # {template}, so the wildcard grammar folds the entire phrase into the template slot.
        (
            "imprime una etiqueta de congelador para lasaña",
            {"template": "congelador", "text": "lasaña"},
        ),
        (
            "imprime una etiqueta de congelador que diga lasaña",
            {"template": "congelador", "text": "lasaña"},
        ),
        # A multi-word name, which no wildcard parse can separate from the dictated text.
        (
            "haz una etiqueta de meal prep para pollo al curry",
            {"template": "meal-prep", "text": "pollo al curry"},
        ),
        # An alias resolves to the canonical name at the grammar layer.
        (
            "haz una etiqueta de comida preparada para pollo",
            {"template": "meal-prep", "text": "pollo"},
        ),
        # Only the FIRST connector is the boundary; the text may contain more.
        (
            "imprime una etiqueta de congelador para sopa para mañana",
            {"template": "congelador", "text": "sopa para mañana"},
        ),
        # Speech-to-text reality: mixed case and punctuation, which the closed list is immune to.
        (
            "Imprime una etiqueta de congelador, que diga lasaña.",
            {"template": "congelador", "text": "lasaña"},
        ),
        # No text at all, for a template that needs none.
        ("imprime una etiqueta de hoy", {"template": "hoy"}),
        # A connector inside a name, spoken with no text. The wildcard grammar already gets this
        # one right (the handler matches the full name exactly before splitting), so this case
        # guards against the closed list REGRESSING it, not against a pre-existing bug.
        ("haz una etiqueta de regalo para navidad", {"template": "regalo-para-navidad"}),
        # The same name WITH dictated text — the case the wildcard grammar genuinely cannot
        # resolve (pinned in test_the_wildcard_grammar_cannot_resolve... below). Both closed-list
        # parses bind one wildcard and match the same literals, so hassil's third criterion
        # decides: less text captured by the wildcard, which is the longer name plus "juan".
        (
            "haz una etiqueta de regalo para navidad para juan",
            {"template": "regalo-para-navidad", "text": "juan"},
        ),
        (
            "haz una etiqueta de regalo para navidad que diga juan",
            {"template": "regalo-para-navidad", "text": "juan"},
        ),
        # And the shorter name with dictated text still resolves to the shorter name.
        ("haz una etiqueta de regalo para juan", {"template": "regalo", "text": "juan"}),
    ],
)
def test_the_closed_list_wins_and_separates_the_text(
    utterance: str, expected: dict[str, Any]
) -> None:
    """recognize_best prefers fewer wildcards, then more literal text — which is exactly this.

    No tie-breaking help is needed: the closed-list parse binds one wildcard (``text``) where the
    wildcard parse binds one too but matches one literal less (the connector), and it captures far
    less text in the wildcard. Both preferences point the same way.
    """
    result = _recognize(_both_documents(), utterance)
    assert result is not None, utterance
    assert _slots(result) == expected


@pytest.mark.parametrize(
    "utterance",
    [
        # Mis-heard name: nothing in the closed list matches, so the wildcard sentences take it and
        # the handler's fuzzy matcher is what rescues it.
        "imprime una etiqueta de cogelador para lasaña",
        # A template that is not in the catalog at all.
        "imprime una etiqueta de pantry para tomato soup",
    ],
)
def test_an_unmatched_name_falls_through_to_the_wildcard(utterance: str) -> None:
    """The floor under the closed list: what it cannot match must still reach the handler."""
    result = _recognize(_both_documents(), utterance)
    assert result is not None, utterance
    slots = _slots(result)
    # Over-captured on purpose — this is the shape _split_template_and_text recovers from.
    assert "para" in slots["template"]
    assert "text" not in slots


@pytest.mark.parametrize(
    "utterance",
    [
        "haz una etiqueta de regalo para navidad para juan",
        "haz una etiqueta de regalo para navidad que diga juan",
    ],
)
def test_the_wildcard_grammar_cannot_resolve_a_connector_in_a_name_with_text(
    utterance: str,
) -> None:
    """The limitation the closed list removes, pinned so the improvement is not just a claim.

    Everything lands in one over-captured slot, and the handler splits at the FIRST connector — so
    a name that itself contains one loses its tail to the dictated text and resolves to the
    shorter template. The exact-name shortcut cannot save it here, because the slot now holds the
    name *plus* the text and matches no name exactly.

    The same utterances resolve correctly through the closed list (parametrized above); that pair
    is what makes this a measured improvement rather than a claim.
    """
    from custom_components.labelito.intents import _split_template_and_text

    result = _recognize([wildcard_document("es")], utterance)
    assert result is not None
    spoken = _slots(result)["template"]

    template, text = _split_template_and_text(spoken, CATALOG)
    assert template is not None
    assert template["name"] == "regalo"
    assert text is not None and text.startswith("navidad")


def test_an_empty_closed_list_is_harmless() -> None:
    """Pin what makes writing the file on an empty catalog the right call.

    If `values: []` raised, or poisoned recognition for the language, the opt-in record would have
    to live somewhere else entirely. It does neither: the closed-list sentences just never match
    and the utterance falls through to the wildcard.
    """
    documents = [wildcard_document("es"), closed_list_document("es", [])]
    result = _recognize(documents, "imprime una etiqueta de congelador para lasaña")
    assert result is not None
    assert _slots(result) == {"template": "congelador para lasaña"}


def test_each_file_is_a_valid_grammar_on_its_own() -> None:
    """Why the closed-list sentences and their list ship together in ONE file.

    hassil raises MissingListError for a sentence referencing an undefined list, and it raises at
    recognition time — so a file holding those sentences while the list lived elsewhere would take
    down every result for the language, including the wildcard sentences meant to be the fallback.
    Each document therefore has to stand alone, and this is what proves it does.
    """
    values, _ = _closed_list_values(CATALOG)
    for document in (wildcard_document("es"), closed_list_document("es", values)):
        result = _recognize([document], "imprime una etiqueta de congelador para lasaña")
        assert result is not None
        assert result.entities["template"].value in ("congelador", "congelador para lasaña")


def test_a_missing_list_would_break_every_sentence() -> None:
    """Pin the hassil behaviour the two-file split is a workaround for.

    If a future hassil downgraded this to "that sentence does not match", the split would stop
    being necessary — and this failing is how we would find out, instead of carrying the
    complexity forever on a stale assumption.
    """
    hassil = pytest.importorskip("hassil")

    values, _ = _closed_list_values(CATALOG)
    broken = closed_list_document("es", values)
    del broken["lists"][TEMPLATE_LIST]
    with pytest.raises(hassil.errors.MissingListError):
        _recognize([broken, wildcard_document("es")], "imprime una etiqueta de congelador")
