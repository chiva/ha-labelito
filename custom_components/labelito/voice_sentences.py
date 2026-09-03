# SPDX-License-Identifier: MIT
"""Generate Home Assistant custom-sentence files from the live labelito template catalog.

Voice printing matches a spoken template name against the catalog. Two grammars can do that, and
this module exists because each is wrong on its own:

* A **wildcard** ``{template}`` slot matches anything, so it works for a catalog nobody enumerated
  in advance — but Home Assistant's ``recognize_best`` then folds the *whole* utterance into that
  slot for any language whose sentences have no literal after it (Spanish: *"imprime una etiqueta
  de congelador para lasaña"* arrives as ``template="congelador para lasaña"``), and the dictated
  text has to be dug back out in :mod:`.intents`.
* A **closed list** of the actual template names matches only what exists — so the text separates
  cleanly at the grammar layer, multi-word names work, and speech-to-text punctuation and casing
  stop mattering — but it is stale the moment a template is added, and it can never cover a name
  the speech-to-text engine mangled.

So both are shipped, and ``recognize_best`` picks between them per utterance. It sorts by *fewer
wildcards first, then more literal text matched*, which is exactly the preference we want and needs
no tie-breaking help: the closed-list parse of *"…de congelador para lasaña"* binds one wildcard
(``text``) and one more literal (*para*) than the wildcard parse, so it wins; an utterance the
closed list cannot match falls to the wildcard sentences and the fuzzy matcher in :mod:`.intents`
rescues it. **A stale or missing list therefore degrades, never breaks** — which is what makes
regenerating it safe to automate.

Two files, because the split is forced rather than chosen
---------------------------------------------------------
``hassil`` raises ``MissingListError`` when a sentence references a list that is not defined, and it
raises it at *recognition* time — killing every result for that language, wildcard sentences
included. A single file holding the closed-list sentences while the generated list lived elsewhere
would take all voice control down whenever the list was absent. So:

* ``labelito.yaml`` — the wildcard grammar. Written once **if it does not exist**, then never
  touched: it is the user's to customize.
* ``labelito-templates.yaml`` — the closed-list sentences *and* the list they reference, together.
  Rewritten whenever the catalog changes. Delete it to opt out.

Each file defines exactly the lists its own sentences use, so either one alone is a valid grammar.
Home Assistant reads every ``*.yaml`` under ``<config>/custom_sentences/<language>/`` and merges
them, appending each file's sentence groups.
"""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import voluptuous as vol
import yaml
from homeassistant.const import SERVICE_RELOAD
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_AMBIGUOUS,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_CONFLICTS,
    ATTR_LANGUAGE,
    ATTR_RELOADED,
    ATTR_SPOKEN_FORMS,
    ATTR_UNCHANGED,
    ATTR_UNSPEAKABLE,
    ATTR_WRITTEN,
    DOMAIN,
    INTENT_PRINT,
    SERVICE_WRITE_VOICE_SENTENCES,
)
from .intents import ambiguous_spoken_forms, resolvable_spoken_forms, unsayable_template_names
from .services import resolve_coordinator

if TYPE_CHECKING:
    from .coordinator import LabelitoCoordinator

_LOGGER = logging.getLogger(__name__)

CUSTOM_SENTENCES_DIR: Final = "custom_sentences"
# The wildcard grammar, matching the file shipped in the repository's custom_sentences/ folder.
WILDCARD_FILENAME: Final = "labelito.yaml"
# The generated closed-list grammar. A distinct file so regenerating it can never clobber sentences
# the user wrote, and so deleting it is a complete, obvious opt-out.
CLOSED_LIST_FILENAME: Final = "labelito-templates.yaml"

# Home Assistant's conversation integration owns custom sentences; reloading it re-reads them from
# disk (default_agent.async_reload clears the per-language cache), so a regenerated file takes
# effect without a restart. Referenced by name rather than imported: the integration is an optional
# after_dependency, and a hard import would make voice a requirement for printing labels.
CONVERSATION_DOMAIN: Final = "conversation"

# Home Assistant merges every custom-sentence file for a language into ONE dictionary, so `lists`
# is a shared namespace across files — this integration's, the user's, and any other integration's.
# Every list defined here is therefore prefixed with the domain, and hassil's ``{list:slot}`` form
# maps it back onto the plain slot the intent handler expects, so the schema is unchanged and
# neither grammar needs a branch on which one matched.
#
# Measured, with a generic name: another custom-sentence file defining its own `template_name` list
# merges its values into ours, and "imprime una etiqueta de tele" resolved to template="television"
# — a value from a grammar that has nothing to do with labels. A `text` list is even likelier to be
# claimed by somebody else, and merging a wildcard declaration with a values list produces a
# `{"wildcard": True, "values": [...]}` hybrid neither side asked for.
TEMPLATE_LIST: Final = f"{DOMAIN}_template_name"
WILDCARD_TEMPLATE_LIST: Final = f"{DOMAIN}_template"
TEXT_LIST: Final = f"{DOMAIN}_text"

# The slot placeholder used in SENTENCE_GROUPS, and what each grammar substitutes for it.
WILDCARD_SLOT: Final = "{template}"
WILDCARD_TEMPLATE_SLOT: Final = f"{{{WILDCARD_TEMPLATE_LIST}:template}}"
CLOSED_LIST_SLOT: Final = f"{{{TEMPLATE_LIST}:template}}"
TEXT_SLOT: Final = "{text}"
NAMESPACED_TEXT_SLOT: Final = f"{{{TEXT_LIST}:text}}"

# One source for both grammars: the closed-list sentences are these with the slot substituted, so
# the two can never drift into matching different phrasings. Kept byte-identical to the shipped
# custom_sentences/<lang>/labelito.yaml (a test pins that), because the integration cannot read
# that folder at runtime — HACS installs custom_components/ only.
#
# Each language's groups are (with-text, without-text). The order between groups is irrelevant:
# recognize_best scores results, it does not prefer earlier sentences, and Home Assistant merges
# the files in filesystem order anyway.
SENTENCE_GROUPS: Final[dict[str, tuple[tuple[str, ...], ...]]] = {
    "en": (
        (
            "print [a] {template} label [for] {text}",
            "print [a] {template} label [that says] {text}",
            "make [a] {template} label [for] {text}",
            "make [a] {template} label [that says] {text}",
        ),
        (
            "print [a] {template} label",
            "make [a] {template} label",
        ),
    ),
    "es": (
        (
            "imprime una etiqueta [de] {template} para {text}",
            "imprime una etiqueta [de] {template} que diga {text}",
            "haz una etiqueta [de] {template} para {text}",
            "haz una etiqueta [de] {template} que diga {text}",
        ),
        (
            "imprime una etiqueta [de] {template}",
            "haz una etiqueta [de] {template}",
        ),
    ),
}
SUPPORTED_LANGUAGES: Final = tuple(SENTENCE_GROUPS)

# Characters hassil's sentence-template parser treats as syntax: group, optional, list and rule
# delimiters, the alternative and permutation separators, and its escape character. Checked against
# the SPOKEN form, because that is the string written to a list value's `in:` and therefore the one
# hassil parses as a template — `out:` is a plain value and is never parsed.
#
# Not cosmetic. Measured against hassil 3.12: an `in:` of "frio (mucho)" silently matches "frio
# mucho"; "frio|calor" becomes two alternatives; and "frio {name}" raises MissingListError — the
# exact failure that discards EVERY result for the language, which is what the two-file split
# exists to prevent, reached here through a template alias instead of a missing file.
#
# Both sources can carry these. A template NAME can because labelito only constrains the names it
# saves itself, so a YAML file placed in its templates directory by hand may be named anything. An
# ALIAS should not — labelito validates aliases against a spoken-name charset — but this
# integration talks to a service it does not own, and the consequence of trusting that is a dead
# voice assistant.
HASSIL_METACHARACTERS: Final = frozenset("()[]{}<>|;\\")

# The first line of every generated file, and the ONLY evidence that a file is this integration's
# to rewrite. Overwriting is gated on it because the alternative — treating any file with the
# expected name as ours — silently destroys a hand-written grammar that happens to be named the
# same, and the automatic refresh would do it at startup with no service call involved. Kept
# byte-stable across versions: changing it orphans every file already on disk.
GENERATED_MARKER: Final = "# GENERATED by the Home Assistant labelito integration."

GENERATED_HEADER: Final = f"""\
{GENERATED_MARKER} Do not edit: it is rewritten whenever the
# template catalog changes, and your changes would be lost.
#
# It adds a closed list of the template names (and their aliases) that labelito currently serves,
# so a spoken name is matched exactly instead of being guessed out of a wildcard. Deleting this
# file is a complete opt-out — voice printing keeps working through the wildcard sentences in
# {WILDCARD_FILENAME}, just less precisely.
#
# To add your own sentences on top of this list, put them in a THIRD file in this directory and
# reference {CLOSED_LIST_SLOT} — the list defined here is available to every file
# Home Assistant merges.
"""

WILDCARD_HEADER: Final = f"""\
# The labelito voice grammar. Written once by the labelito integration if it was missing; it is
# YOURS to edit from here on and is never overwritten.
#
# {{template}} and {{text}} are wildcards, so any template name is matched and mis-heard names are
# resolved by the integration's fuzzy matcher. Run the labelito.write_voice_sentences service to
# also generate {CLOSED_LIST_FILENAME}, which matches the names you actually have exactly.
"""


SERVICE_WRITE_VOICE_SENTENCES_SCHEMA = vol.Schema(
    {
        # Restricted to the languages a grammar exists for: a value outside it could only ever
        # write a file Home Assistant would load and then fail to match anything from.
        vol.Optional(ATTR_LANGUAGE): vol.All(cv.ensure_list, [vol.In(SUPPORTED_LANGUAGES)]),
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    }
)


# Serializes the whole read-catalog-then-write-file sequence, across every path that performs it.
#
# It has to span BOTH steps, which is why the refresh takes a coordinator instead of a catalog. Each
# path awaits between taking its snapshot and committing it, so the event loop can run the other one
# in between: the background refresh reads the cached catalog, the service then force-fetches a
# newer one and writes it, and the background refresh finally commits ITS older snapshot on top —
# atomically, so the newer list is simply gone. A lock around the write alone would not help; the
# inversion is decided by when each snapshot was taken.
#
# Self-correcting but not quickly: the next background refresh notices the content differs and
# rewrites, which is up to TEMPLATE_CACHE_TTL (15 minutes) later. Until then the closed list is a
# catalog behind and a newly added template resolves through the wildcard fallback instead — right
# label, just not the exact match the user asked for by running the service.
_CATALOG_TO_DISK = asyncio.Lock()


@dataclass(frozen=True)
class SentencesResult:
    """What one write pass did, as reported by the ``write_voice_sentences`` service."""

    # Paths relative to the Home Assistant config directory, so a response can be shown or logged
    # without leaking the absolute layout of the host.
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    # Number of spoken forms in the closed list: every template name plus every alias that survived
    # the checks below.
    spoken_forms: int = 0
    # Forms more than one template claims. Dropped, because there is no way to tell which was
    # meant, and reported because the only fix is renaming a template or an alias.
    ambiguous: list[str] = field(default_factory=list)
    # Forms left out because they cannot be a spoken form at all: sentence-grammar syntax hassil
    # would parse instead of match (HASSIL_METACHARACTERS), or nothing left after normalization.
    unspeakable: list[str] = field(default_factory=list)
    # Files with a generated file's NAME that this integration did not write, so it will not touch
    # them. Reported because the consequence is silent: while one is in place the automatic refresh
    # sees no file of its own and does nothing at all.
    conflicts: list[str] = field(default_factory=list)
    reloaded: bool = False


def _closed_list_values(templates: list[dict[str, Any]]) -> tuple[list[dict[str, str]], list[str]]:
    """Build the list's ``in``/``out`` pairs from every spoken form, and the forms left out.

    ``in`` is what a person says; ``out`` is the canonical template name, which is what the intent
    handler receives and prints. Sorted, so an unchanged catalog produces a byte-identical file and
    nothing is rewritten or reloaded for no reason.

    :func:`.intents.resolvable_spoken_forms` supplies the vocabulary, already reduced to the forms
    the handler can resolve. The only exclusion made here is this module's own concern: a spoken
    form carrying sentence-grammar syntax, which hassil would parse instead of match — or, for a
    list reference, refuse to recognize anything at all with. Like every other exclusion it falls
    back to the wildcard sentences rather than failing.
    """
    values: list[dict[str, str]] = []
    # Names that normalize to nothing never reach the index, so they are collected at the source
    # rather than filtered here — but they belong in the same bucket: the template exists and voice
    # cannot reach it, and a rename is the fix either way.
    unspeakable: list[str] = unsayable_template_names(templates)
    for spoken, name in sorted(resolvable_spoken_forms(templates).items()):
        if HASSIL_METACHARACTERS.intersection(spoken):
            # Reported by the spoken form, since that is what has to change — for a name it is the
            # name, but for an alias the name is fine and the alias is the problem.
            unspeakable.append(spoken)
            continue
        values.append({"in": spoken, "out": name})
    return values, sorted(set(unspeakable))


def _document(
    language: str, groups: tuple[tuple[str, ...], ...], lists: dict[str, Any]
) -> dict[str, Any]:
    return {
        "language": language,
        "intents": {INTENT_PRINT: {"data": [{"sentences": list(group)} for group in groups]}},
        "lists": lists,
    }


def _substitute(language: str, template_slot: str) -> tuple[tuple[str, ...], ...]:
    """SENTENCE_GROUPS with the template and text placeholders replaced by namespaced slots."""
    return tuple(
        tuple(
            sentence.replace(WILDCARD_SLOT, template_slot).replace(TEXT_SLOT, NAMESPACED_TEXT_SLOT)
            for sentence in group
        )
        for group in SENTENCE_GROUPS[language]
    )


def wildcard_document(language: str) -> dict[str, Any]:
    """The wildcard grammar: any template name, with the text dug back out by the handler."""
    return _document(
        language,
        _substitute(language, WILDCARD_TEMPLATE_SLOT),
        {WILDCARD_TEMPLATE_LIST: {"wildcard": True}, TEXT_LIST: {"wildcard": True}},
    )


def closed_list_document(language: str, values: list[dict[str, str]]) -> dict[str, Any]:
    """The closed-list grammar: the same sentences over a list of the names that actually exist.

    Defines the text list as well as the name list. Both files declaring it is deliberate: each
    file must be a valid grammar on its own, because either can be absent, and Home Assistant's
    merge collapses the two identical declarations.
    """
    return _document(
        language,
        _substitute(language, CLOSED_LIST_SLOT),
        {TEMPLATE_LIST: {"values": values}, TEXT_LIST: {"wildcard": True}},
    )


def _dump(header: str, document: dict[str, Any]) -> str:
    # sort_keys=False keeps `language` / `intents` / `lists` in reading order; allow_unicode keeps
    # accented template names and aliases readable instead of \\u-escaped.
    return header + yaml.safe_dump(
        document, allow_unicode=True, sort_keys=False, default_flow_style=False
    )


def _atomic_write(path: Path, content: str) -> None:
    """Replace ``path`` with ``content`` in one step, leaving the old file intact on any failure.

    A plain ``write_text`` truncates the live file and then fills it, and Home Assistant loads
    whatever is in ``custom_sentences/`` at the next reload or restart. A file caught inside that
    window is not merely ignored: a truncated closed-list file can still parse as a mapping whose
    sentences reference the list its cut-off tail was supposed to define, which is the
    MissingListError case that discards EVERY result for the language. Since this also runs from
    the automatic refresh, nobody would be watching when it happened.

    So the content is staged in the target's own directory — the rename must be a rename, not a
    cross-device copy — flushed and fsynced so the bytes are really on disk before the swap, then
    moved into place, which is atomic. The staging name is unique per call because the service and
    the background refresh can both be writing, and carries a ``.tmp`` suffix rather than
    ``.yaml`` so Home Assistant's ``*.yaml`` scan cannot pick a half-written file up even in the
    instant it exists.
    """
    fd, staged_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    staged = Path(staged_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        staged.replace(path)
    finally:
        # A no-op after a successful replace (the name is gone); on failure it is the cleanup that
        # keeps a dead .tmp from accumulating in the user's config directory.
        staged.unlink(missing_ok=True)


# What makes an existing generated file impossible to COMPARE against: unreadable, or not valid
# UTF-8. Named rather than written inline because `ruff format` renders an inline tuple as
# `except OSError, UnicodeDecodeError:` — legal here (this integration is Python 3.14 only, where
# PEP 758 allows it, and CI proves it compiles) but the same spelling meant `as` in Python 2, and it
# already drew a false "SyntaxError, module cannot be imported" from an automated reviewer. A single
# name reads the same to every Python and every tool.
_UNREADABLE = (OSError, UnicodeDecodeError)


def _is_generated(path: Path) -> bool:
    """True when ``path`` provably came from this integration, by its marker line.

    Read as BYTES and prefix-matched rather than decoded, so a truncated or otherwise undecodable
    file that still carries the header is recognized as ours and gets replaced — while arbitrary
    content is not. That distinction is what lets "replace a corrupted generated file" and "never
    touch a file we cannot prove we wrote" both hold; deciding it by decoding would collapse them
    into one rule and force a bad choice between them.
    """
    try:
        return path.read_bytes().startswith(GENERATED_MARKER.encode())
    except OSError:
        return False


def _opted_in(directories: list[Path]) -> list[Path]:
    """Which of ``directories`` hold a generated file this integration may refresh (executor).

    Runs BEFORE the catalog is touched, which is the point: a refresh for a language nobody opted
    into should do no work at all — not derive a vocabulary and then decline to write it. At
    startup that derivation ran optional voice code where an exception aborts the config entry,
    for a user who never enabled generated sentences.
    """
    return [
        directory for directory in directories if _is_generated(directory / CLOSED_LIST_FILENAME)
    ]


def _write_files(jobs: list[tuple[Path, str, bool]]) -> tuple[list[Path], list[Path]]:
    """Write the files that need it, inside the executor. Returns (written, conflicts).

    ``jobs`` is (path, content, replace_if_ours):

    * ``False`` — write only when the file is absent. That is how the user's copy of the wildcard
      grammar survives, corrupted or not: a file we do not own is not ours to replace.
    * ``True`` — write when absent, or when the existing file carries our marker and its content
      differs. An unchanged file is left alone, so an unchanged catalog costs neither a rewrite nor
      a conversation reload.

    An existing file that is NOT ours is reported as a conflict rather than replaced or silently
    skipped. Silence would be the worst of the three: because the file's existence doubles as the
    opt-in record, an unrecognized file means the refresh will never do anything at all, and only
    saying so makes that discoverable.
    """
    written: list[Path] = []
    conflicts: list[Path] = []
    for path, content, replace_if_ours in jobs:
        if path.exists():
            if not replace_if_ours:
                continue
            if not _is_generated(path):
                conflicts.append(path)
                continue
            try:
                if path.read_text(encoding="utf-8") == content:
                    continue
            except _UNREADABLE:
                # Ours by its marker but undecodable, so a comparison is impossible and rewriting
                # is the right answer. Caught rather than propagated because UnicodeDecodeError is
                # a ValueError, NOT an OSError: it would have escaped the callers' error handling
                # and, at startup, taken the config entry down over a corrupted cache file about
                # to be overwritten anyway.
                pass
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content)
        written.append(path)
    return written, conflicts


async def _async_reload_conversation(hass: HomeAssistant) -> bool:
    """Ask the conversation integration to re-read custom sentences. False if it could not be done.

    Without this a regenerated file only takes effect on the next restart, because the default
    agent caches the parsed intents per language.

    A failure here is reported, never raised — including an unexpected one. This is a call into
    another integration's service made purely as a convenience, after our files are already on
    disk, and the fallback is the restart the user would otherwise have needed. Letting it
    propagate would mean an unrelated conversation problem — an invalid custom-sentence file
    somebody hand-edited, a broken agent — could abort labelito's setup and stop the printer
    working, which is a strictly worse outcome than "the new sentences need a restart".
    ``CancelledError`` derives from BaseException, so a shutdown still cancels cleanly.
    """
    if not hass.services.has_service(CONVERSATION_DOMAIN, SERVICE_RELOAD):
        return False
    try:
        await hass.services.async_call(CONVERSATION_DOMAIN, SERVICE_RELOAD, blocking=True)
    except Exception as err:
        _LOGGER.warning(
            "Wrote the voice sentence files but could not reload the conversation integration "
            "(%s); they take effect after a Home Assistant restart",
            err,
        )
        return False
    return True


async def async_write_voice_sentences(
    hass: HomeAssistant,
    templates: list[dict[str, Any]],
    languages: list[str] | None = None,
    *,
    create: bool,
) -> SentencesResult:
    """Write the voice grammar for ``languages`` (default: all supported) from ``templates``.

    ``create=True`` is the explicit opt-in, used by the service: it creates both files, including
    the wildcard grammar if the user never installed it by hand. ``create=False`` is the automatic
    refresh: it updates a generated file that already exists and creates nothing. That asymmetry is
    what keeps the integration from writing into the config directory unbidden — nothing appears
    under ``custom_sentences/`` until the service is called once, and after that it stays current
    on its own.

    Raises ``OSError`` if the config directory cannot be written. The service reports that to the
    user who asked for it; the automatic refresh goes through
    :func:`async_refresh_voice_sentences`, which swallows everything.
    """
    by_directory = {
        Path(hass.config.path(CUSTOM_SENTENCES_DIR, language)): language
        for language in languages or SUPPORTED_LANGUAGES
    }
    if not create:
        # Ownership decides the opt-in, and it is decided BEFORE the catalog is touched — see
        # _opted_in. In the executor because it reads files.
        opted_in = await hass.async_add_executor_job(_opted_in, list(by_directory))
        if not opted_in:
            return SentencesResult()
        by_directory = {directory: by_directory[directory] for directory in opted_in}

    values, unspeakable = _closed_list_values(templates)
    jobs: list[tuple[Path, str, bool]] = []
    for directory, language in by_directory.items():
        if create:
            jobs.append(
                (
                    directory / WILDCARD_FILENAME,
                    _dump(WILDCARD_HEADER, wildcard_document(language)),
                    False,
                )
            )
        # Written even when there are no values, which is what makes the file's existence a
        # reliable record of the opt-in. Skipping it on an empty catalog looked harmless and was
        # not: `create=True` on an empty catalog wrote only the wildcard file, so every later
        # `create=False` refresh saw no generated file, concluded the user had not opted in, and
        # never wrote one again — templates added afterwards silently never got an exact grammar.
        # An empty list is also not the broken grammar the earlier comment here claimed. Measured
        # against hassil 3.12: `values: []` parses, its sentences simply never match, and the
        # utterance falls through to the wildcard — the designed degradation, not a failure.
        jobs.append(
            (
                directory / CLOSED_LIST_FILENAME,
                _dump(GENERATED_HEADER, closed_list_document(language, values)),
                True,
            )
        )

    written, conflicts = await hass.async_add_executor_job(_write_files, jobs)
    config_dir = Path(hass.config.config_dir)

    def _relative(paths: list[Path]) -> list[str]:
        return sorted(str(path.relative_to(config_dir)) for path in paths)

    untouched = [
        path for path, _content, _replace in jobs if path not in written and path not in conflicts
    ]
    return SentencesResult(
        written=_relative(written),
        unchanged=_relative(untouched),
        spoken_forms=len(values),
        ambiguous=ambiguous_spoken_forms(templates),
        unspeakable=unspeakable,
        conflicts=_relative(conflicts),
        reloaded=bool(written) and await _async_reload_conversation(hass),
    )


async def async_sync_voice_sentences(
    coordinator: LabelitoCoordinator,
    languages: list[str] | None = None,
    *,
    create: bool,
    force_refresh: bool,
) -> SentencesResult:
    """Read the catalog and commit it to disk as ONE unit, serialized against every other caller.

    The only way production code should reach :func:`async_write_voice_sentences`. That function
    takes a catalog it is handed, so on its own it cannot know whether the snapshot is still the
    newest one — and both paths that use it await between snapshotting and committing, which is
    all the event loop needs to interleave them: the refresh snapshots catalog A, the service
    force-fetches catalog B and writes it, and the refresh then commits A on top, atomically.

    So the fetch happens in here, under :data:`_CATALOG_TO_DISK`, and callers hand over a
    coordinator instead of a catalog. Putting the lock at the call sites instead was tried and is
    worse in the way that matters: it holds only for the callers that remember it, and the first
    one that does not silently restores the race.
    """
    async with _CATALOG_TO_DISK:
        templates = await coordinator.async_get_templates(force_refresh=force_refresh)
        return await async_write_voice_sentences(
            coordinator.hass, templates, languages, create=create
        )


async def async_refresh_voice_sentences(coordinator: LabelitoCoordinator) -> None:
    """Re-sync an opted-in grammar from the catalog, best effort. NEVER raises.

    Used by both callers that are not a user asking for it: config-entry setup and the background
    task behind the TTL template refresh. That second one runs off the utterance path deliberately
    — the intent handler forces its own catalog refresh on a miss, and rewriting sentence files
    plus reloading the conversation agent mid-utterance would be surprising and pointless, since
    the fuzzy matcher already resolves a name the closed list has not caught up with.

    Takes the coordinator rather than a catalog so the fetch happens INSIDE
    :data:`_CATALOG_TO_DISK`; see that lock for why a caller-supplied snapshot is the bug.

    Swallowing is broad on purpose. The input is a catalog served over HTTP by a service this
    integration does not own, and the code it drives writes files and calls into another
    integration — so the set of things it can raise is not one this module can enumerate, and none
    of them are a reason to stop printing labels. Setup used to catch only OSError, which meant a
    schema-drifted template response could abort the config entry from optional voice code, for a
    user who never enabled generated sentences. The grammar already on disk keeps working, and the
    next refresh retries. ``CancelledError`` derives from BaseException, so shutdown still cancels
    cleanly.
    """
    try:
        await async_sync_voice_sentences(coordinator, create=False, force_refresh=False)
    except Exception as err:
        _LOGGER.warning("Could not refresh the voice sentence files: %s", err)


def async_setup_voice_sentences_service(hass: HomeAssistant) -> None:
    """Register ``labelito.write_voice_sentences``.

    Registered from this module rather than :mod:`.services` because the dependency runs that way:
    generating a grammar needs the matcher's vocabulary (:mod:`.intents`), which needs the print
    execution helpers. The same reason :func:`.intents.async_setup_intents` registers its own
    intent handler.
    """

    async def _handle_write_voice_sentences(call: ServiceCall) -> ServiceResponse:
        coordinator = resolve_coordinator(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
        try:
            # force_refresh because this service is called precisely after adding or renaming a
            # template, which is exactly when the cached catalog is the wrong answer. Through
            # async_sync_voice_sentences so the fetch and the write are one serialized unit — a
            # concurrent background refresh must not commit an older catalog over this one.
            result = await async_sync_voice_sentences(
                coordinator, call.data.get(ATTR_LANGUAGE), create=True, force_refresh=True
            )
        except OSError as err:
            raise HomeAssistantError(
                f"Could not write the voice sentence files under "
                f"{Path(hass.config.path(CUSTOM_SENTENCES_DIR))}: {err}"
            ) from err

        if not call.return_response:
            return None
        # Annotated dict[str, Any] rather than the narrower JsonObjectType: list[str] is not a
        # list[JsonValueType] under invariance, and every value here is already JSON-safe.
        response: dict[str, Any] = {
            ATTR_WRITTEN: result.written,
            ATTR_UNCHANGED: result.unchanged,
            ATTR_SPOKEN_FORMS: result.spoken_forms,
            ATTR_AMBIGUOUS: result.ambiguous,
            ATTR_UNSPEAKABLE: result.unspeakable,
            ATTR_CONFLICTS: result.conflicts,
            ATTR_RELOADED: result.reloaded,
        }
        return response

    hass.services.async_register(
        DOMAIN,
        SERVICE_WRITE_VOICE_SENTENCES,
        _handle_write_voice_sentences,
        schema=SERVICE_WRITE_VOICE_SENTENCES_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
