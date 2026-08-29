from __future__ import annotations

import base64
import json
from io import BytesIO

import httpx
import pytest
from fieldledger.providers.base import (
    ApprovalRequest,
    EditRequest,
    ExportRequest,
    ItemApproval,
    ProviderMutationUnknown,
    ProviderOutcomeStatus,
    ProviderPayloadError,
    ProviderProblem,
)
from fieldledger.providers.deterministic import DeterministicDocumentProvider
from fieldledger.providers.superdocs import SuperDocsProvider

pytestmark = pytest.mark.asyncio


def _client(handler: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=handler,
        base_url="https://api.superdocs.app",
    )


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"id": "j", "status": "pending"}, ProviderOutcomeStatus.PROCESSING),
        ({"id": "j", "status": "in_progress"}, ProviderOutcomeStatus.PROCESSING),
        (
            {
                "id": "j",
                "status": "awaiting_input",
                "metadata": {"awaiting_kind": "continue_prompt"},
            },
            ProviderOutcomeStatus.PROCESSING,
        ),
        ({"id": "j", "status": "awaiting_approval"}, ProviderOutcomeStatus.REVIEW_READY),
        ({"id": "j", "status": "PreviewReady"}, ProviderOutcomeStatus.PREVIEW_READY),
        (
            {
                "id": "j",
                "status": "in_progress",
                "proposed_changes": [{"id": "c", "content": {"after": "new"}}],
            },
            ProviderOutcomeStatus.REVIEW_READY,
        ),
        ({"id": "j", "status": "completed"}, ProviderOutcomeStatus.COMPLETED),
        ({"id": "j", "status": "failed"}, ProviderOutcomeStatus.FAILED),
        ({"id": "j", "status": "cancelled"}, ProviderOutcomeStatus.CANCELLED),
    ],
)
async def test_live_adapter_normalizes_every_loading_and_terminal_shape(
    payload: dict[str, object], expected: ProviderOutcomeStatus
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/jobs/j"
        return httpx.Response(200, json=payload)

    async with _client(httpx.MockTransport(handler)) as client:
        outcome = await SuperDocsProvider(
            api_key="placeholder-test-secret",
            client=client,
        ).poll("j")

    assert outcome.status is expected
    assert outcome.is_loading is (expected is ProviderOutcomeStatus.PROCESSING)


async def test_live_adapter_double_parses_change_content_and_prefers_final_body() -> None:
    payload = {
        "id": "j",
        "status": "PreviewReady",
        "proposed_changes": [
            {
                "id": "c1",
                "content": json.dumps(
                    {"before": "old", "after": "new", "reason": "contract delta"}
                ),
            }
        ],
        "session_events": [{"type": "insert", "content": "incremental"}],
        "document": {"id": "d1", "version": "v2", "content": "final document"},
    }

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    async with _client(httpx.MockTransport(handler)) as client:
        outcome = await SuperDocsProvider(
            api_key="placeholder-test-secret",
            client=client,
        ).poll("j")

    assert outcome.proposed_changes[0].content == {
        "before": "old",
        "after": "new",
        "reason": "contract delta",
    }
    assert outcome.document is not None
    assert outcome.document.content == b"final document"
    assert outcome.incremental_events == ()


@pytest.mark.parametrize("content", ["not-json", "[]", "42", json.dumps("[]")])
async def test_live_adapter_rejects_malformed_or_non_object_proposals(content: str) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "j",
                "status": "awaiting_approval",
                "proposed_changes": [{"id": "c", "content": content}],
            },
        )

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderPayloadError, match="proposed change"):
            await SuperDocsProvider(
                api_key="placeholder-test-secret",
                client=client,
            ).poll("j")


@pytest.mark.parametrize(
    ("status", "kind"),
    [
        (400, "bad_request"),
        (401, "unauthorized"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (413, "payload_too_large"),
        (415, "unsupported_media_type"),
        (422, "unprocessable"),
        (429, "rate_limited"),
        (500, "provider_error"),
        (504, "provider_timeout"),
    ],
)
async def test_live_adapter_maps_provider_errors_without_payload_or_secret_leak(
    status: int, kind: str
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"Retry-After": "7"},
            json={"error": "sensitive upstream payload"},
        )

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderProblem) as raised:
            await SuperDocsProvider(
                api_key="placeholder-test-secret",
                client=client,
            ).poll("j")

    assert raised.value.status == status
    assert raised.value.kind == kind
    assert raised.value.retry_after_seconds == 7
    rendered = str(raised.value)
    assert "placeholder-test-secret" not in rendered
    assert "sensitive upstream payload" not in rendered


async def test_live_start_and_approve_send_documented_safe_payloads() -> None:
    requests: list[tuple[str, dict[str, object], str | None]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body, request.headers.get("Idempotency-Key")))
        assert request.headers["authorization"] == "Bearer placeholder-test-secret"
        return httpx.Response(
            200,
            json={
                "job_id": "job-1",
                "session_id": "doc-1",
                "job_type": "chat",
                "status": "pending",
                "created_at": "2026-08-28T00:00:00Z",
                "updated_at": "2026-08-28T00:00:00Z",
                "progress": 0,
            },
        )

    async with _client(httpx.MockTransport(handler)) as client:
        provider = SuperDocsProvider(
            api_key="placeholder-test-secret",
            client=client,
        )
        await provider.start_edit(
            EditRequest(
                document_id="doc-1",
                prompt="revise",
                idempotency_key="edit-idem-1",
            )
        )
        await provider.approve(
            ApprovalRequest(
                job_id="job-1",
                document_id="doc-1",
                approved=False,
                changes=(
                    ItemApproval(change_id="a", approved=True),
                    ItemApproval(change_id="b", approved=False, note="wrong amount"),
                ),
                idempotency_key="idem-1",
            )
        )

    assert requests[0] == (
        "/v1/chat/async",
        {"session_id": "doc-1", "message": "revise", "approval_mode": "ask_every_time"},
        "edit-idem-1",
    )
    assert requests[1][0] == "/v1/chat/doc-1/approve"
    assert requests[1][1]["job_id"] == "job-1"
    assert requests[1][1]["approved"] is False
    assert requests[1][1]["changes"] == [
        {"change_id": "a", "approved": True, "feedback": None},
        {"change_id": "b", "approved": False, "feedback": "wrong amount"},
    ]


async def test_live_upload_and_export_use_the_session_contract() -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append((request.url.path, body))
        if request.url.path == "/v1/documents/upload-base64":
            return httpx.Response(
                200,
                json={
                    "session_id": body["session_id"],
                    "version_id": "version-1",
                    "html": "<p>Field report</p>",
                },
            )
        return httpx.Response(
            200,
            headers={
                "content-type": (
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                )
            },
            content=b"exported-docx",
        )

    async with _client(httpx.MockTransport(handler)) as client:
        provider = SuperDocsProvider(api_key="placeholder-test-secret", client=client)
        document = await provider.upload(BytesIO(b"plain report"), idempotency_key="upload-1")
        exported = await provider.export(ExportRequest(document_id=document.id, format="docx"))

    upload_path, upload = requests[0]
    assert upload_path == "/v1/documents/upload-base64"
    assert upload["filename"] == "document.txt"
    assert upload["session_id"] == document.id
    assert upload["return_html"] is True
    assert base64.b64decode(str(upload["file_base64"])) == b"plain report"
    assert requests[1] == (
        "/v1/documents/export",
        {"session_id": document.id, "format": "docx"},
    )
    assert exported.content == b"exported-docx"


async def test_live_large_upload_uses_presigned_storage_without_bearer_leak() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/uploads":
            body = json.loads(request.content)
            assert body["size_bytes"] == 100_001
            return httpx.Response(
                200,
                json={
                    "upload_id": "upload-1",
                    "upload_url": "https://storage.example/upload-1",
                    "expires_at": "2026-08-28T00:05:00Z",
                    "expires_in_seconds": 300,
                    "max_size_bytes": 104_857_600,
                    "curl_example": "omitted",
                },
            )
        if request.url.host == "storage.example":
            assert "authorization" not in request.headers
            assert request.content == b"x" * 100_001
            return httpx.Response(200)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "session_id": body["session_id"],
                "filename": body["filename"],
                "status": "completed",
                "parse_mode": "document",
                "version_id": "version-large",
                "html": "<p>Large report</p>",
            },
        )

    async with _client(httpx.MockTransport(handler)) as client:
        document = await SuperDocsProvider(api_key="placeholder-test-secret", client=client).upload(
            BytesIO(b"x" * 100_001), idempotency_key="large-upload"
        )

    assert paths == ["/v1/uploads", "/upload-1", "/v1/uploads/upload-1/process"]
    assert document.version == "version-large"


async def test_live_start_timeout_is_ambiguous_and_replay_uses_same_provider_key() -> None:
    seen_keys: list[str | None] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen_keys.append(request.headers.get("Idempotency-Key"))
        if len(seen_keys) == 1:
            raise httpx.ReadTimeout("provider accepted edit but response was lost")
        return httpx.Response(200, json={"job_id": "job-replayed", "status": "pending"})

    async with _client(httpx.MockTransport(handler)) as client:
        provider = SuperDocsProvider(
            api_key="placeholder-test-secret",
            client=client,
        )
        request = EditRequest(
            document_id="doc-1",
            prompt="revise",
            idempotency_key="edit-timeout-1",
        )
        with pytest.raises(ProviderMutationUnknown):
            await provider.start_edit(request)
        replay = await provider.start_edit(request)

    assert replay.id == "job-replayed"
    assert seen_keys == ["edit-timeout-1", "edit-timeout-1"]


async def test_network_timeout_after_mutation_is_never_guessed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lost response")

    async with _client(httpx.MockTransport(handler)) as client:
        provider = SuperDocsProvider(
            api_key="placeholder-test-secret",
            client=client,
        )
        with pytest.raises(ProviderMutationUnknown):
            await provider.approve(
                ApprovalRequest(
                    job_id="job-1",
                    document_id="doc-1",
                    approved=True,
                    changes=(ItemApproval(change_id="a", approved=True),),
                    idempotency_key="idem-1",
                )
            )


async def test_gateway_timeout_after_mutation_is_never_guessed() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(504, headers={"Retry-After": "9"})

    async with _client(httpx.MockTransport(handler)) as client:
        provider = SuperDocsProvider(
            api_key="placeholder-test-secret",
            client=client,
        )
        with pytest.raises(ProviderMutationUnknown):
            await provider.approve(
                ApprovalRequest(
                    job_id="job-1",
                    document_id="doc-1",
                    approved=True,
                    changes=(ItemApproval(change_id="item-key", approved=True),),
                    idempotency_key="idem-504",
                )
            )


async def test_live_read_preserves_pending_changes_and_session_events() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/documents"):
            assert request.url.params["include_html"] == "true"
            return httpx.Response(
                200,
                json={
                    "documents": [
                        {
                            "document_id": "doc-primary",
                            "version": "provider-v7",
                            "html": "current",
                            "focused": True,
                        }
                    ]
                },
            )
        if request.url.path.endswith("/jobs"):
            return httpx.Response(
                200,
                json={
                    "jobs": [
                        {
                            "job_id": "job-7",
                            "status": "awaiting_approval",
                            "metadata": {
                                "pending_changes": [
                                    {
                                        "change_id": "amount",
                                        "operation": "edit",
                                        "old_html": "<td>1</td>",
                                        "new_html": "<td>2</td>",
                                        "ai_explanation": "updated total",
                                    }
                                ]
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={"events": [{"id": 7, "type": "document_updated"}]},
        )

    async with _client(httpx.MockTransport(handler)) as client:
        state = await SuperDocsProvider(
            api_key="placeholder-test-secret",
            client=client,
        ).read_document("doc-1")

    assert state.version == "provider-v7"
    assert state.document_id == "doc-primary"
    assert [change.id for change in state.pending_changes] == ["amount"]
    assert state.pending_changes[0].content == {
        "before": "<td>1</td>",
        "after": "<td>2</td>",
        "reason": "updated total",
        "operation": "edit",
        "document_id": None,
        "chunk_id": None,
    }
    assert state.unexpected_events == ({"id": 7, "type": "document_updated"},)


async def test_retry_after_accepts_http_date() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={
                "Date": "Sun, 23 Aug 2026 12:00:00 GMT",
                "Retry-After": "Sun, 23 Aug 2026 12:00:07 GMT",
            },
        )

    async with _client(httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderProblem) as raised:
            await SuperDocsProvider(
                api_key="placeholder-test-secret",
                client=client,
            ).poll("job-1")

    assert raised.value.retry_after_seconds == 7


async def test_deterministic_provider_is_stable_no_key_and_supports_repeated_review() -> None:
    provider = DeterministicDocumentProvider()
    first = await provider.upload(BytesIO(b"before"), idempotency_key="upload-1")
    replay = await provider.upload(BytesIO(b"ignored"), idempotency_key="upload-1")
    assert first == replay

    job = await provider.start_edit(
        EditRequest(document_id=first.id, prompt="replace", idempotency_key="edit-1")
    )
    review = await provider.poll(job.id)
    assert review.status is ProviderOutcomeStatus.REVIEW_READY
    change_id = review.proposed_changes[0].id

    another_round = await provider.approve(
        ApprovalRequest(
            job_id=job.id,
            document_id=first.id,
            approved=False,
            changes=(ItemApproval(change_id=change_id, approved=False, note="revise"),),
            idempotency_key="decision-1",
        )
    )
    assert another_round.status is ProviderOutcomeStatus.REVIEW_READY

    completed = await provider.approve(
        ApprovalRequest(
            job_id=job.id,
            document_id=first.id,
            approved=True,
            changes=(
                ItemApproval(
                    change_id=another_round.proposed_changes[0].id,
                    approved=True,
                ),
            ),
            idempotency_key="decision-2",
        )
    )
    assert completed.status is ProviderOutcomeStatus.COMPLETED
    exported = await provider.export(ExportRequest(document_id=first.id, format="docx"))
    assert exported.content


async def test_deterministic_provider_rejects_unknown_change_ids() -> None:
    provider = DeterministicDocumentProvider()
    document = await provider.upload(BytesIO(b"before"), idempotency_key="upload-unknown")
    job = await provider.start_edit(
        EditRequest(
            document_id=document.id,
            prompt="replace",
            idempotency_key="edit-unknown",
        )
    )

    with pytest.raises(ValueError, match="unknown provider change"):
        await provider.approve(
            ApprovalRequest(
                job_id=job.id,
                document_id=document.id,
                approved=True,
                changes=(ItemApproval(change_id="database-row-uuid", approved=True),),
                idempotency_key="decision-unknown",
            )
        )
