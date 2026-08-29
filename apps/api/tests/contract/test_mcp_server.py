from __future__ import annotations

import json
from dataclasses import dataclass, field
from uuid import UUID

import pytest
from fieldledger.api.dependencies import ServiceContainer
from fieldledger.domain.review import ApplyResult
from fieldledger.mcp_server import create_mcp_server
from fieldledger.security import ConfirmationTokenError, SecurityManager
from mcp.shared.memory import create_connected_server_and_client_session

pytestmark = pytest.mark.asyncio
PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
OTHER_PROJECT_ID = UUID("99999999-9999-4999-8999-999999999999")
BATCH_ID = UUID("22222222-2222-4222-8222-222222222222")
RUN_ID = UUID("33333333-3333-4333-8333-333333333333")
DOCUMENT_ID = UUID("44444444-4444-4444-8444-444444444444")


@dataclass
class McpServiceFake:
    calls: list[str] = field(default_factory=list)

    async def ensure_project(self, *, project_id, name: str, description: str | None, actor, role):  # type: ignore[no-untyped-def]
        self.calls.append("create_project")
        return {"id": str(project_id), "name": name, "status": "active"}

    async def add_fixture_source(self, project_id, *, fixture_name: str, actor):  # type: ignore[no-untyped-def]
        self.calls.append("add_fixture_source")
        return {"project_id": str(project_id), "source_name": fixture_name, "status": "parsed"}

    async def start_run(self, project_id, *, actor, idempotency_key: str):  # type: ignore[no-untyped-def]
        self.calls.append("start_run")
        return {"id": str(RUN_ID), "project_id": str(project_id), "status": "processing"}

    async def inspect_run(self, run_id, actor):  # type: ignore[no-untyped-def]
        self.calls.append("inspect_run")
        return {"id": str(run_id), "status": "review_ready"}

    async def list_reviews(self, project_id, actor, *, limit: int, offset: int, statuses=None):  # type: ignore[no-untyped-def]
        self.calls.append("list_pending_reviews")
        return ([{"id": str(BATCH_ID), "project_id": str(project_id), "status": "open"}], 1)

    async def submit_decisions(self, batch_id, command, actor, *, now=None):  # type: ignore[no-untyped-def]
        if command.confirmation_token != "ui-signed-token":
            raise ConfirmationTokenError("confirmation token signature is invalid")
        self.calls.append("submit_review_decisions")
        return ApplyResult(status="completed", batch_id=batch_id)

    async def list_audit(self, project_id, actor, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        self.calls.append("inspect_audit")
        return ([{"event_type": "workflow.stage_completed"}], 1)

    async def export_document(self, document_id, *, format: str, actor):  # type: ignore[no-untyped-def]
        self.calls.append("export")
        return {"document_id": str(document_id), "status": "completed", "format": format}

    async def start_edit(self, *, document_id, workflow_run_id, prompt, idempotency_key, actor):  # type: ignore[no-untyped-def]
        self.calls.append("start_edit")
        return {"id": str(RUN_ID), "status": "processing"}

    async def poll(self, job_id, actor):  # type: ignore[no-untyped-def]
        self.calls.append("poll_provider_job")
        return {"id": str(job_id), "status": "review_ready", "review_batch_id": str(BATCH_ID)}


def server(fake: McpServiceFake):  # type: ignore[no-untyped-def]
    services = ServiceContainer(
        projects=fake,
        ingestion=fake,
        workflow=fake,
        reviews=fake,
        audit=fake,
        providers=fake,
        exports=fake,
    )
    security = SecurityManager(
        signing_secret="placeholder-mcp-secret",
        issuer="fieldledger-mcp-test",
        audience="fieldledger-mcp-client",
        local_demo_enabled=False,
    )
    return create_mcp_server(
        services=services,
        security=security,
        actor_id="mcp-agent-test",
        allowed_project_id=PROJECT_ID,
    )


def content(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    assert not result.isError
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


async def test_official_in_memory_client_lists_exact_tools_and_runs_valid_e2e() -> None:
    fake = McpServiceFake()
    async with create_connected_server_and_client_session(server(fake)) as client:
        listed = await client.list_tools()
        assert {tool.name for tool in listed.tools} == {
            "create_project",
            "add_fixture_source",
            "start_run",
            "inspect_run",
            "start_edit",
            "poll_provider_job",
            "list_pending_reviews",
            "request_human_confirmation",
            "submit_review_decisions",
            "inspect_audit",
            "export",
        }
        project = content(
            await client.call_tool("create_project", {"name": "Site Alpha"})
        )
        assert project["id"] == str(PROJECT_ID)
        source = content(
            await client.call_tool(
                "add_fixture_source",
                {"project_id": str(PROJECT_ID), "fixture_name": "site_report.txt"},
            )
        )
        assert source["status"] == "parsed"
        run = content(
            await client.call_tool(
                "start_run",
                {"project_id": str(PROJECT_ID), "idempotency_key": "mcp-run-1"},
            )
        )
        assert run["status"] == "processing"
        inspected = content(
            await client.call_tool(
                "inspect_run",
                {"project_id": str(PROJECT_ID), "run_id": str(RUN_ID)},
            )
        )
        assert inspected["status"] == "review_ready"
        reviews = content(
            await client.call_tool(
                "list_pending_reviews", {"project_id": str(PROJECT_ID)}
            )
        )
        assert reviews["items"][0]["id"] == str(BATCH_ID)
        audit = content(
            await client.call_tool(
                "inspect_audit", {"project_id": str(PROJECT_ID), "limit": 25}
            )
        )
        assert audit["items"][0]["event_type"] == "workflow.stage_completed"
        exported = content(
            await client.call_tool(
                "export",
                {
                    "project_id": str(PROJECT_ID),
                    "document_id": str(DOCUMENT_ID),
                    "format": "docx",
                },
            )
        )
        assert exported["status"] == "completed"

    assert fake.calls == [
        "create_project",
        "add_fixture_source",
        "start_run",
        "inspect_run",
        "list_pending_reviews",
        "inspect_audit",
        "export",
    ]


async def test_invalid_schema_and_cross_project_are_protocol_errors_without_service_calls() -> None:
    fake = McpServiceFake()
    async with create_connected_server_and_client_session(server(fake)) as client:
        invalid = await client.call_tool(
            "start_run",
            {"project_id": 7, "idempotency_key": "bad"},
        )
        cross_project = await client.call_tool(
            "list_pending_reviews",
            {"project_id": str(OTHER_PROJECT_ID)},
        )

    assert invalid.isError
    assert cross_project.isError
    assert fake.calls == []


async def test_mcp_confirmation_is_ui_action_only_and_cannot_claim_reviewer_authority() -> None:
    fake = McpServiceFake()
    async with create_connected_server_and_client_session(server(fake)) as client:
        requested = content(
            await client.call_tool(
                "request_human_confirmation",
                {"project_id": str(PROJECT_ID), "batch_id": str(BATCH_ID)},
            )
        )
        attempted = await client.call_tool(
            "submit_review_decisions",
            {
                "project_id": str(PROJECT_ID),
                "batch_id": str(BATCH_ID),
                "idempotency_key": "mcp-decision-1",
                "confirmation_token": "claimed-ui-token",
                "claimed_role": "reviewer",
                "decisions": [],
            },
        )

    assert requested == {
        "status": "ui_action_required",
        "batch_id": str(BATCH_ID),
        "action": "request_confirmation_in_ui",
    }
    assert attempted.isError
    assert fake.calls == []


async def test_mcp_relays_ui_token_and_rejects_forged_or_missing_token() -> None:
    fake = McpServiceFake()
    async with create_connected_server_and_client_session(server(fake)) as client:
        relayed = content(
            await client.call_tool(
                "submit_review_decisions",
                {
                    "project_id": str(PROJECT_ID),
                    "batch_id": str(BATCH_ID),
                    "idempotency_key": "mcp-relay-valid",
                    "confirmation_token": "ui-signed-token",
                    "decisions": [],
                },
            )
        )
        forged = await client.call_tool(
            "submit_review_decisions",
            {
                "project_id": str(PROJECT_ID),
                "batch_id": str(BATCH_ID),
                "idempotency_key": "mcp-relay-forged",
                "confirmation_token": "forged-token",
                "decisions": [],
            },
        )
        missing = await client.call_tool(
            "submit_review_decisions",
            {
                "project_id": str(PROJECT_ID),
                "batch_id": str(BATCH_ID),
                "idempotency_key": "mcp-relay-missing",
                "decisions": [],
            },
        )

    assert relayed["status"] == "completed"
    assert forged.isError
    assert missing.isError
    assert fake.calls == ["submit_review_decisions"]
