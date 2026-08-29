from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any, BinaryIO, cast
from urllib.parse import quote

import httpx

from fieldledger.providers.base import (
    ApprovalRequest,
    EditRequest,
    ExportedFile,
    ExportRequest,
    ProposedChange,
    ProviderDocument,
    ProviderDocumentState,
    ProviderJob,
    ProviderMutationUnknown,
    ProviderOutcome,
    ProviderOutcomeStatus,
    ProviderPayloadError,
    ProviderProblem,
)

_ERROR_KINDS = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "unprocessable",
    429: "rate_limited",
    500: "provider_error",
    504: "provider_timeout",
}
_INLINE_UPLOAD_LIMIT = 100_000
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


class SuperDocsProvider:
    def __init__(
        self,
        *,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 15,
    ) -> None:
        if not api_key:
            raise ValueError("SuperDocs API key is required for the live provider")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(base_url="https://api.superdocs.app")
        self._timeout = httpx.Timeout(timeout_seconds)

    async def upload(self, source: BinaryIO, *, idempotency_key: str) -> ProviderDocument:
        content = source.read()
        if not isinstance(content, bytes):
            raise ProviderPayloadError("document source must contain bytes")
        if not content:
            raise ProviderPayloadError("document source is empty")
        if len(content) > _MAX_UPLOAD_BYTES:
            raise ProviderProblem(status=413, kind="payload_too_large")

        filename, media_type = self._file_info(content)
        session_id = f"fieldledger_{sha256(idempotency_key.encode()).hexdigest()[:32]}"
        if len(content) > _INLINE_UPLOAD_LIMIT:
            payload = await self._upload_large(
                content,
                filename=filename,
                media_type=media_type,
                session_id=session_id,
            )
        else:
            response = await self._request(
                "POST",
                "/v1/documents/upload-base64",
                headers={"Idempotency-Key": idempotency_key},
                json={
                    "filename": filename,
                    "file_base64": base64.b64encode(content).decode("ascii"),
                    "session_id": session_id,
                    "return_html": True,
                },
            )
            payload = self._object(response.json(), "document")
        return ProviderDocument(
            id=str(payload.get("session_id") or session_id),
            version=str(
                payload.get("version_id")
                or payload.get("version")
                or payload.get("content_hash")
                or sha256(content).hexdigest()
            ),
            media_type=media_type,
            content=str(payload.get("html") or "").encode(),
        )

    async def start_edit(self, request: EditRequest) -> ProviderJob:
        try:
            response = await self._request(
                "POST",
                "/v1/chat/async",
                headers={"Idempotency-Key": request.idempotency_key},
                json={
                    "session_id": request.document_id,
                    "message": request.prompt,
                    "approval_mode": "ask_every_time",
                },
                mutation=True,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ProviderMutationUnknown("provider edit response was not confirmed") from error
        payload = self._object(response.json(), "job")
        return ProviderJob(
            id=str(payload.get("job_id") or payload.get("id") or ""),
            document_id=request.document_id,
            status=self._status(payload),
            retry_after_seconds=self._retry_after(response),
        )

    async def poll(self, job_id: str) -> ProviderOutcome:
        response = await self._request("GET", f"/v1/jobs/{job_id}")
        return self._outcome(self._object(response.json(), "job"), response)

    async def approve(self, request: ApprovalRequest) -> ProviderOutcome:
        try:
            response = await self._request(
                "POST",
                f"/v1/chat/{quote(request.document_id, safe='')}/approve",
                headers={"Idempotency-Key": request.idempotency_key},
                json={
                    "job_id": request.job_id,
                    "approved": request.approved,
                    "changes": [
                        {
                            "change_id": item.change_id,
                            "approved": item.approved,
                            "feedback": item.note,
                        }
                        for item in request.changes
                    ],
                },
                mutation=True,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ProviderMutationUnknown("provider mutation response was not confirmed") from error
        return self._outcome(self._object(response.json(), "approval outcome"), response)

    async def read_document(self, document_id: str) -> ProviderDocumentState:
        session_id = quote(document_id, safe="")
        documents_response = await self._request(
            "GET",
            f"/v1/sessions/{session_id}/documents",
            params={"include_html": "true"},
        )
        documents_value = documents_response.json()
        if isinstance(documents_value, dict):
            documents_value = documents_value.get("documents", [])
        if not isinstance(documents_value, list) or not documents_value:
            raise ProviderPayloadError("session document roster is empty")
        documents = [self._object(item, "session document") for item in documents_value]
        focused = next((item for item in documents if item.get("focused")), documents[0])
        document = self._document(focused)

        jobs_response = await self._request(
            "GET",
            f"/v1/sessions/{session_id}/jobs",
            params={"limit": 1, "compact": "false"},
        )
        jobs_payload = jobs_response.json()
        jobs_value = (
            jobs_payload.get("jobs", []) if isinstance(jobs_payload, dict) else jobs_payload
        )
        if not isinstance(jobs_value, list):
            raise ProviderPayloadError("session jobs must be a list")
        latest_job = self._object(jobs_value[0], "session job") if jobs_value else None
        metadata = (
            self._object(latest_job.get("metadata") or {}, "job metadata")
            if latest_job is not None
            else {}
        )
        pending_changes = self._changes(metadata.get("pending_changes", []))

        events_response = await self._request(
            "GET",
            f"/v1/sessions/{session_id}/doc-events",
            params={"after_id": 0, "include_own": "false"},
        )
        events_payload = events_response.json()
        events_value = (
            events_payload.get("events", []) if isinstance(events_payload, dict) else events_payload
        )
        return ProviderDocumentState(
            document_id=document.id,
            version=document.version,
            media_type=document.media_type,
            content=document.content,
            last_outcome=(
                self._status(latest_job, has_changes=bool(pending_changes))
                if latest_job is not None
                else None
            ),
            pending_changes=pending_changes,
            unexpected_events=self._events(events_value),
        )

    async def export(self, request: ExportRequest) -> ExportedFile:
        response = await self._request(
            "POST",
            "/v1/documents/export",
            json={"session_id": request.document_id, "format": request.format},
        )
        content_type = response.headers.get("content-type", "application/octet-stream")
        if content_type.startswith("application/json"):
            payload = self._object(response.json(), "export")
            encoded = payload.get("content_base64")
            if not isinstance(encoded, str):
                raise ProviderPayloadError("export content is missing")
            content = base64.b64decode(encoded, validate=True)
            name = str(payload.get("name") or f"{request.document_id}.{request.format}")
            media_type = str(payload.get("media_type") or "application/octet-stream")
        else:
            content = response.content
            name = f"{request.document_id}.{request.format}"
            media_type = content_type
        return ExportedFile(name=name, media_type=media_type, content=content)

    async def _upload_large(
        self,
        content: bytes,
        *,
        filename: str,
        media_type: str,
        session_id: str,
    ) -> dict[str, Any]:
        reservation = await self._request(
            "POST",
            "/v1/uploads",
            json={
                "filename": filename,
                "content_type": media_type,
                "size_bytes": len(content),
                "purpose": "document",
            },
        )
        reservation_payload = self._object(reservation.json(), "upload reservation")
        upload_id = str(reservation_payload.get("upload_id") or "")
        upload_url = str(reservation_payload.get("upload_url") or "")
        if not upload_id or not upload_url:
            raise ProviderPayloadError("upload reservation is incomplete")
        try:
            stored = await self._client.request(
                "PUT",
                upload_url,
                headers={"Content-Type": media_type},
                content=content,
                timeout=self._timeout,
            )
        except (httpx.TimeoutException, httpx.NetworkError) as error:
            raise ProviderProblem(status=504, kind="provider_timeout") from error
        if stored.status_code >= 400:
            raise ProviderProblem(
                status=stored.status_code,
                kind=_ERROR_KINDS.get(stored.status_code, "provider_error"),
            )
        processed = await self._request(
            "POST",
            f"/v1/uploads/{quote(upload_id, safe='')}/process",
            json={
                "session_id": session_id,
                "filename": filename,
                "parse_mode": "document",
                "return_html": True,
            },
        )
        return self._object(processed.json(), "processed document")

    @staticmethod
    def _file_info(content: bytes) -> tuple[str, str]:
        if content.startswith(b"%PDF-"):
            return "document.pdf", "application/pdf"
        if content.startswith(b"PK\x03\x04"):
            # ponytail: add source filenames to the protocol if non-DOCX ZIPs matter.
            return (
                "document.docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        if content.lstrip().startswith(b"<"):
            return "document.html", "text/html"
        return "document.txt", "text/plain"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        mutation: bool = False,
        **kwargs: Any,
    ) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._client.request(
                method,
                path,
                headers=headers,
                timeout=self._timeout,
                **kwargs,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            if mutation:
                raise
            raise ProviderProblem(status=504, kind="provider_timeout") from None
        if response.status_code >= 400:
            if mutation and response.status_code in {502, 503, 504}:
                raise ProviderMutationUnknown("provider mutation response was not confirmed")
            raise ProviderProblem(
                status=response.status_code,
                kind=_ERROR_KINDS.get(response.status_code, "provider_error"),
                retry_after_seconds=self._retry_after(response),
            )
        return response

    def _outcome(self, payload: dict[str, Any], response: httpx.Response) -> ProviderOutcome:
        metadata = payload.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ProviderPayloadError("job metadata must be an object")
        changes_value = payload.get("proposed_changes", metadata.get("pending_changes", []))
        result = payload.get("result") or {}
        if not isinstance(result, dict):
            raise ProviderPayloadError("job result must be an object")
        document_changes = result.get("document_changes") or {}
        if not isinstance(document_changes, dict):
            raise ProviderPayloadError("document changes must be an object")
        if not changes_value:
            changes_value = document_changes.get("pending_changes", [])
        changes = self._changes(changes_value)
        status = self._status(payload, has_changes=bool(changes))
        document_payload = payload.get("document")
        if document_payload is None and (
            document_changes.get("updated_html") is not None
            or document_changes.get("version_id") is not None
        ):
            document_payload = {
                "document_id": payload.get("session_id"),
                "version_id": document_changes.get("version_id"),
                "html": document_changes.get("updated_html") or "",
                "media_type": "text/html",
            }
        document = (
            self._document(self._object(document_payload, "document"))
            if document_payload is not None
            else None
        )
        events = payload.get("session_events", [])
        normalized_events: tuple[dict[str, Any], ...] = ()
        if document is None:
            if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
                raise ProviderPayloadError("session events must be objects")
            normalized_events = tuple(cast(dict[str, Any], item) for item in events)
        return ProviderOutcome(
            job_id=str(payload.get("id") or payload.get("job_id") or "unknown"),
            status=status,
            proposed_changes=changes,
            document=document,
            incremental_events=normalized_events,
            retry_after_seconds=self._retry_after(response),
        )

    @staticmethod
    def _status(payload: dict[str, Any], *, has_changes: bool = False) -> ProviderOutcomeStatus:
        raw = str(payload.get("status", "pending"))
        normalized = raw.lower()
        awaiting_kind = str(
            (payload.get("metadata") or {}).get("awaiting_kind", "")
            if isinstance(payload.get("metadata") or {}, dict)
            else ""
        )
        if awaiting_kind == "continue_prompt":
            return ProviderOutcomeStatus.PROCESSING
        if normalized == "previewready":
            return ProviderOutcomeStatus.PREVIEW_READY
        if normalized == "awaiting_approval" or has_changes:
            return ProviderOutcomeStatus.REVIEW_READY
        if normalized in {"pending", "in_progress", "awaiting_input", "processing", "success"}:
            return ProviderOutcomeStatus.PROCESSING
        mapping = {
            "completed": ProviderOutcomeStatus.COMPLETED,
            "failed": ProviderOutcomeStatus.FAILED,
            "cancelled": ProviderOutcomeStatus.CANCELLED,
        }
        try:
            return mapping[normalized]
        except KeyError as error:
            raise ProviderPayloadError(f"unhandled provider status: {raw}") from error

    @classmethod
    def _changes(cls, value: Any) -> tuple[ProposedChange, ...]:
        if value is None:
            return ()
        if not isinstance(value, list):
            raise ProviderPayloadError("proposed changes must be a list")
        changes: list[ProposedChange] = []
        for item in value:
            data = cls._object(item, "proposed change")
            if "content" not in data and ("change_id" in data or "old_html" in data):
                content: Any = {
                    "before": data.get("old_html") or "",
                    "after": data.get("new_html") or "",
                    "reason": data.get("ai_explanation") or "provider proposal",
                    "operation": data.get("operation"),
                    "document_id": data.get("document_id"),
                    "chunk_id": data.get("chunk_id"),
                }
            else:
                content = data.get("content", {})
            parses = 0
            while isinstance(content, str) and parses < 2:
                try:
                    content = json.loads(content)
                except json.JSONDecodeError as error:
                    raise ProviderPayloadError("proposed change content is malformed") from error
                parses += 1
            if not isinstance(content, dict):
                raise ProviderPayloadError("proposed change content must be an object")
            changes.append(
                ProposedChange(
                    id=str(data.get("change_id") or data.get("id") or ""),
                    content=content,
                )
            )
        return tuple(changes)

    @staticmethod
    def _events(value: Any) -> tuple[dict[str, Any], ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
            raise ProviderPayloadError("session events must be objects")
        return tuple(cast(dict[str, Any], item) for item in value)

    @staticmethod
    def _document(payload: dict[str, Any]) -> ProviderDocument:
        content = payload.get("content", payload.get("html", b""))
        if isinstance(content, str):
            content = content.encode()
        if not isinstance(content, bytes):
            raise ProviderPayloadError("document content must be text or bytes")
        return ProviderDocument(
            id=str(payload.get("id") or payload.get("document_id") or ""),
            version=str(
                payload.get("version")
                or payload.get("version_id")
                or payload.get("content_hash")
                or "unknown"
            ),
            media_type=str(payload.get("media_type") or "text/html"),
            content=content,
        )

    @staticmethod
    def _object(value: Any, label: str) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ProviderPayloadError(f"{label} payload must be an object")
        return cast(dict[str, Any], value)

    @staticmethod
    def _optional_string(value: Any) -> str | None:
        return str(value) if value is not None else None

    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                target = parsedate_to_datetime(value)
                baseline_value = response.headers.get("Date")
                baseline = (
                    parsedate_to_datetime(baseline_value)
                    if baseline_value is not None
                    else datetime.now(UTC)
                )
                return max(0.0, (target - baseline).total_seconds())
            except (TypeError, ValueError, OverflowError):
                return None
