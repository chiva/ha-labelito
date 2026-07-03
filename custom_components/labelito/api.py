# SPDX-License-Identifier: MIT
"""Async HTTP client for the labelito API.

Deliberately free of Home Assistant imports (aiohttp only) so it can be exercised standalone.
Endpoint paths, request bodies, and error-detail shapes mirror labelito's app/main.py and
app/models.py exactly.
"""

from __future__ import annotations

from typing import Any

import aiohttp

PATH_HEALTH = "/health"
PATH_PRINTER_STATUS = "/printer/status"
PATH_TEMPLATES = "/templates"
PATH_PRINT = "/print"
PATH_REPRINT = "/reprint/{job_id}"

# Read endpoints answer immediately; a print renders and sends a raster to physical hardware
# (a sequence batch sends one label at a time), so it gets a much longer budget.
READ_TIMEOUT = aiohttp.ClientTimeout(total=15)
PRINT_TIMEOUT = aiohttp.ClientTimeout(total=120)

HTTP_UNAUTHORIZED = 401


class LabelitoError(Exception):
    """Base error for the labelito client."""


class LabelitoConnectionError(LabelitoError):
    """The labelito service could not be reached."""


class LabelitoAuthError(LabelitoError):
    """The API token was rejected (HTTP 401)."""


class LabelitoApiError(LabelitoError):
    """A non-success API response, carrying the status code and the FastAPI ``detail`` payload.

    ``detail`` preserves labelito's shape verbatim: a plain string for simple errors
    (404 template/job not found, 409 idempotency-key reuse) or a dict for structured ones
    (409 media mismatch: msg/label/media_required/media_loaded; 409 fault: msg/errors/media_loaded;
    422 missing fields: msg/template/missing_required; 503 busy: msg/phase or msg/errors).
    """

    def __init__(self, status: int, detail: str | dict[str, Any] | list[Any]) -> None:
        super().__init__(f"Labelito API error {status}: {detail}")
        self.status = status
        self.detail = detail


class LabelitoClient:
    """Minimal typed client over labelito's HTTP API."""

    def __init__(
        self,
        host: str,
        port: int,
        api_token: str | None,
        session: aiohttp.ClientSession,
    ) -> None:
        self._base_url = f"http://{host}:{port}"
        self._api_token = api_token
        self._session = session

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        # labelito auth is `Authorization: Bearer <API_TOKEN>` (HTTPBearer in app/main.py).
        if self._api_token:
            return {"Authorization": f"Bearer {self._api_token}"}
        return {}

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        timeout: aiohttp.ClientTimeout = READ_TIMEOUT,
        ok_statuses: tuple[int, ...] = (200,),
    ) -> Any:
        try:
            async with self._session.request(
                method,
                f"{self._base_url}{path}",
                json=json_body,
                headers=self._headers(),
                timeout=timeout,
            ) as response:
                if response.status in ok_statuses:
                    return await response.json()
                if response.status == HTTP_UNAUTHORIZED:
                    raise LabelitoAuthError("Invalid or missing API token")
                detail = await self._extract_detail(response)
                raise LabelitoApiError(response.status, detail)
        except TimeoutError as err:
            raise LabelitoConnectionError(f"Timeout talking to {self._base_url}") from err
        except aiohttp.ClientError as err:
            raise LabelitoConnectionError(f"Cannot connect to {self._base_url}: {err}") from err

    @staticmethod
    async def _extract_detail(response: aiohttp.ClientResponse) -> str | dict[str, Any] | list[Any]:
        # FastAPI wraps every HTTPException payload as {"detail": <str | dict | list>}; fall back
        # to the raw text for non-JSON bodies (e.g. the body-size middleware's plain responses).
        try:
            body = await response.json()
        except aiohttp.ContentTypeError, ValueError:
            return await response.text()
        if isinstance(body, dict) and "detail" in body:
            return body["detail"]
        return body

    async def health(self) -> dict[str, Any]:
        """GET /health — HealthResponse: status, version, api_version, driver, model, transport,
        uri, template_count, default_language, languages. Unauthenticated."""
        result: dict[str, Any] = await self._request("GET", PATH_HEALTH)
        return result

    async def printer_status(self) -> dict[str, Any]:
        """GET /printer/status — PrinterStatusResponse.

        labelito returns 503 with a full response body (``reachable: false``, state ``off`` or
        ``printing``) when the printer is unreachable or busy, so both 200 and 503 are treated as
        data rather than errors — exactly the branching the endpoint's docstring recommends.
        """
        result: dict[str, Any] = await self._request(
            "GET", PATH_PRINTER_STATUS, ok_statuses=(200, 503)
        )
        return result

    async def templates(self) -> list[dict[str, Any]]:
        """GET /templates — list of TemplateInfo: name, description, label, rotate,
        fields {required, optional}, media {width_mm, media_type, length_mm} | null."""
        result: list[dict[str, Any]] = await self._request("GET", PATH_TEMPLATES)
        return result

    async def print_label(self, request: dict[str, Any]) -> dict[str, Any]:
        """POST /print — body is a labelito PrintRequest; returns PrintResponse
        {job_id, template, copies, dry_run}."""
        result: dict[str, Any] = await self._request(
            "POST", PATH_PRINT, json_body=request, timeout=PRINT_TIMEOUT
        )
        return result

    async def reprint(self, job_id: str) -> dict[str, Any]:
        """POST /reprint/{job_id} — replays a recorded job; returns PrintResponse."""
        result: dict[str, Any] = await self._request(
            "POST", PATH_REPRINT.format(job_id=job_id), timeout=PRINT_TIMEOUT
        )
        return result
