"""Tests for the print/reprint execution helpers and service dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.labelito.api import LabelitoApiError, LabelitoClient
from custom_components.labelito.coordinator import LabelitoCoordinator
from custom_components.labelito.services import (
    _build_print_request,
    _speakable_detail,
    _validate_sequence,
    async_execute_print,
    async_reprint_last,
    async_validate_template,
    resolve_coordinator,
)

from .const import BASE_URL, MOCK_HEALTH, MOCK_TEMPLATES


@pytest.fixture
def client() -> AsyncMock:
    mock = AsyncMock(spec=LabelitoClient)
    mock.base_url = BASE_URL
    mock.templates.return_value = list(MOCK_TEMPLATES)
    mock.print_label.return_value = {
        "job_id": "job-1",
        "template": "pantry",
        "copies": 1,
        "dry_run": False,
    }
    return mock


@pytest.fixture
def coordinator(
    hass: HomeAssistant, mock_config_entry: MockConfigEntry, client: AsyncMock
) -> LabelitoCoordinator:
    mock_config_entry.add_to_hass(hass)
    return LabelitoCoordinator(hass, mock_config_entry, client, MOCK_HEALTH)


async def test_validate_template_found(coordinator: LabelitoCoordinator) -> None:
    templates = await async_validate_template(coordinator, "pantry")
    assert any(t["name"] == "pantry" for t in templates)


async def test_validate_template_miss_refreshes_then_raises(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    with pytest.raises(ServiceValidationError, match="Unknown template"):
        await async_validate_template(coordinator, "nope")
    # First miss forces one refresh before giving up.
    assert client.templates.await_count == 2


async def test_execute_print_sets_idempotency_key(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    await async_execute_print(
        coordinator, {"template": "pantry", "fields": {}, "copies": 1, "dry_run": False}
    )
    sent = client.print_label.await_args.args[0]
    assert "idempotency_key" in sent
    assert coordinator.last_job_id == "job-1"
    assert coordinator.ha_printed_count == 1


async def test_execute_print_preserves_caller_idempotency_key(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    """A caller-supplied key survives so a retried print dedupes server-side (no fresh UUID)."""
    await async_execute_print(
        coordinator,
        {
            "template": "pantry",
            "fields": {},
            "copies": 1,
            "dry_run": False,
            "idempotency_key": "stable-key-1",
        },
    )
    sent = client.print_label.await_args.args[0]
    assert sent["idempotency_key"] == "stable-key-1"


async def test_execute_print_replay_does_not_double_count(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    """An idempotency-key replay returns the prior job_id with no physical print — count it once."""
    call = {
        "template": "pantry",
        "fields": {},
        "copies": 1,
        "dry_run": False,
        "idempotency_key": "stable-key-1",
    }
    await async_execute_print(coordinator, dict(call))
    # labelito replays the same job_id for a reused key; the client mock already returns job-1.
    await async_execute_print(coordinator, dict(call))
    assert coordinator.ha_printed_count == 1


async def test_execute_print_dry_run_does_not_count(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    client.print_label.return_value = {
        "job_id": "j",
        "template": "pantry",
        "copies": 1,
        "dry_run": True,
    }
    await async_execute_print(
        coordinator, {"template": "pantry", "fields": {}, "copies": 1, "dry_run": True}
    )
    assert coordinator.ha_printed_count == 0


async def test_execute_print_maps_409_to_validation_error(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    client.print_label.side_effect = LabelitoApiError(
        409,
        {"msg": "mismatch", "media_loaded": "62mm continuous", "media_required": "29x90mm die-cut"},
    )
    with pytest.raises(ServiceValidationError, match="62mm continuous"):
        await async_execute_print(
            coordinator, {"template": "pantry", "fields": {}, "copies": 1, "dry_run": False}
        )


async def test_execute_print_maps_503_to_ha_error(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    client.print_label.side_effect = LabelitoApiError(503, "busy")
    with pytest.raises(HomeAssistantError):
        await async_execute_print(
            coordinator, {"template": "pantry", "fields": {}, "copies": 1, "dry_run": False}
        )


async def test_reprint_without_prior_job_raises(coordinator: LabelitoCoordinator) -> None:
    with pytest.raises(ServiceValidationError, match="Nothing to reprint"):
        await async_reprint_last(coordinator)


async def test_reprint_404_clears_stale_job(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    coordinator.last_job_id = "gone"
    client.reprint = AsyncMock(side_effect=LabelitoApiError(404, "Job not found"))
    with pytest.raises(ServiceValidationError, match="Print a new label"):
        await async_reprint_last(coordinator)
    assert coordinator.last_job_id is None


async def test_reprint_credits_sequence_batch_size(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    # Print a 5-label sequence, then reprint it: the reprint response echoes copies=1, but the
    # counter must credit the remembered batch size (5), and reprint must be told the count so it
    # can scale its timeout.
    await async_execute_print(
        coordinator,
        {
            "template": "crate",
            "fields": {},
            "copies": 1,
            "dry_run": False,
            "sequence": {"count": 5},
        },
    )
    assert coordinator.ha_printed_count == 5
    assert coordinator.last_job_labels == 5
    client.reprint = AsyncMock(
        return_value={"job_id": "job-2", "template": "crate", "copies": 1, "dry_run": False}
    )
    await async_reprint_last(coordinator)
    client.reprint.assert_awaited_once_with("job-1", 5)
    assert coordinator.ha_printed_count == 10


async def test_resolve_coordinator_none_loaded(hass: HomeAssistant) -> None:
    with pytest.raises(ServiceValidationError, match="No labelito printer"):
        resolve_coordinator(hass, None)


def test_build_print_request_omits_unset_options() -> None:
    request = _build_print_request(
        {"template": "pantry", "fields": {"title": "x"}, "copies": 2, "dry_run": False}
    )
    assert request == {
        "template": "pantry",
        "fields": {"title": "x"},
        "copies": 2,
        "dry_run": False,
    }
    assert "language" not in request
    assert "options" not in request


def test_build_print_request_includes_provided_options() -> None:
    request = _build_print_request(
        {
            "template": "pantry",
            "fields": {},
            "copies": 1,
            "dry_run": False,
            "language": "es",
            "cut": True,
            "red": True,
        }
    )
    assert request["language"] == "es"
    assert request["cut"] is True
    assert request["options"] == {"red": True}


def test_build_print_request_passes_through_idempotency_key() -> None:
    request = _build_print_request(
        {
            "template": "pantry",
            "fields": {},
            "copies": 1,
            "dry_run": False,
            "idempotency_key": "stable-key-1",
        }
    )
    assert request["idempotency_key"] == "stable-key-1"


def test_build_print_request_assembles_sequence() -> None:
    request = _build_print_request(
        {
            "template": "crate",
            "fields": {"label": "Widgets"},
            "copies": 1,
            "dry_run": False,
            "seq_count": 50,
            "seq_start": 100,
            "seq_step": 2,
            "seq_padding": 3,
        }
    )
    assert request["sequence"] == {"count": 50, "start": 100, "step": 2, "padding": 3}


def test_build_print_request_sequence_count_only_omits_defaults() -> None:
    # Only seq_count set: the optional knobs are omitted so labelito's SequenceSpec defaults apply.
    request = _build_print_request(
        {"template": "crate", "fields": {}, "copies": 1, "dry_run": False, "seq_count": 10}
    )
    assert request["sequence"] == {"count": 10}


def test_build_print_request_no_sequence_without_count() -> None:
    request = _build_print_request(
        {"template": "pantry", "fields": {}, "copies": 1, "dry_run": False}
    )
    assert "sequence" not in request


def test_validate_sequence_requires_count() -> None:
    with pytest.raises(ServiceValidationError, match="seq_count"):
        _validate_sequence(
            {"template": "crate", "fields": {}, "copies": 1, "dry_run": False, "seq_start": 5}
        )


def test_validate_sequence_rejects_copies_with_count() -> None:
    with pytest.raises(ServiceValidationError, match="mutually exclusive"):
        _validate_sequence(
            {"template": "crate", "fields": {}, "copies": 3, "dry_run": False, "seq_count": 10}
        )


def test_validate_sequence_allows_plain_and_sequence_prints() -> None:
    # No sequence input, and a valid sequence (count with copies == 1) both pass without raising.
    _validate_sequence({"template": "pantry", "fields": {}, "copies": 2, "dry_run": False})
    _validate_sequence(
        {"template": "crate", "fields": {}, "copies": 1, "dry_run": False, "seq_count": 10}
    )


async def test_execute_print_counts_sequence_batch(
    coordinator: LabelitoCoordinator, client: AsyncMock
) -> None:
    # labelito echoes copies=1 for a sequence batch; the counter must credit the sequence count.
    await async_execute_print(
        coordinator,
        {
            "template": "crate",
            "fields": {"label": "x"},
            "copies": 1,
            "dry_run": False,
            "sequence": {"count": 5},
        },
    )
    assert coordinator.ha_printed_count == 5


def test_speakable_detail_media_mismatch() -> None:
    detail = {"media_loaded": "62mm continuous", "media_required": "29x90mm die-cut"}
    assert _speakable_detail(detail) == (
        "The loaded roll is 62mm continuous but the template needs 29x90mm die-cut"
    )
