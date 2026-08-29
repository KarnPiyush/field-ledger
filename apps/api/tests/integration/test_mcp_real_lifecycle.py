from __future__ import annotations

import json
from uuid import UUID, uuid4

import pytest
from fieldledger.agent.stages import DeterministicStageExecutor
from fieldledger.api.dependencies import ServiceContainer
from fieldledger.config import Settings
from fieldledger.mcp_server import create_mcp_server
from fieldledger.persistence import tables
from fieldledger.providers.deterministic import DeterministicDocumentProvider
from fieldledger.security import (
    Actor,
    ActorSource,
    GlobalRole,
    ProjectAuthorizer,
    ProjectRole,
    SecurityManager,
)
from fieldledger.services.dependency_service import DependencyService
from fieldledger.services.export_service import ExportService
from fieldledger.services.ingestion_service import IngestionService
from fieldledger.services.project_service import (
    AuditService,
    ProjectService,
    RunService,
    SourceService,
)
from fieldledger.services.provider_service import ProviderService
from fieldledger.services.review_service import ReviewService
from fieldledger.services.workflow_service import WorkflowService
from fieldledger.worker import run_worker_iteration
from mcp.shared.memory import create_connected_server_and_client_session
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


def _content(result) -> dict[str, object]:  # type: ignore[no-untyped-def]
    assert not result.isError, result.content
    assert len(result.content) == 1
    return json.loads(result.content[0].text)


async def test_real_mcp_fixed_scope_runs_provider_review_relay_audit_and_export() -> None:
    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id = uuid4()
    workflow_core = WorkflowService(sessions)
    ingestion_core = IngestionService(sessions, workflow=workflow_core)
    authorization = ProjectAuthorizer(sessions)
    provider = DeterministicDocumentProvider()
    dependencies = DependencyService(sessions, policy_content_hash="sha256:mcp-policy-v1")
    security = SecurityManager(
        signing_secret="placeholder-real-mcp-signing",
        issuer="fieldledger-real-mcp-test",
        audience="fieldledger-real-mcp-client",
        local_demo_enabled=False,
    )
    providers = ProviderService(
        sessions,
        provider,
        authorization=authorization,
        dependencies=dependencies,
    )
    reviews = ReviewService(
        sessions,
        provider=provider,
        security=security,
        authorization=authorization,
        dependencies=dependencies,
    )
    services = ServiceContainer(
        projects=ProjectService(sessions, authorization),
        ingestion=SourceService(ingestion_core, authorization),
        workflow=RunService(sessions, workflow_core, authorization),
        reviews=reviews,
        audit=AuditService(sessions, authorization),
        providers=providers,
        exports=ExportService(sessions, provider, authorization=authorization),
    )
    server = create_mcp_server(
        services=services,
        security=security,
        actor_id="real-mcp-agent",
        allowed_project_id=project_id,
    )
    human = Actor(
        actor_id="real-mcp-human",
        source=ActorSource.UI,
        global_role=GlobalRole.USER,
    )
    try:
        async with create_connected_server_and_client_session(server) as client:
            tools = {tool.name for tool in (await client.list_tools()).tools}
            assert {"start_edit", "poll_provider_job"} <= tools
            created = _content(
                await client.call_tool(
                    "create_project",
                    {"name": "MCP fixed project", "description": "real lifecycle"},
                )
            )
            assert created["id"] == str(project_id)
            async with sessions.begin() as session:
                await session.execute(
                    insert(tables.project_memberships).values(
                        project_id=project_id,
                        actor_id=human.actor_id,
                        role=ProjectRole.REVIEWER.value,
                        granted_by="test",
                    )
                )

            source = _content(
                await client.call_tool(
                    "add_fixture_source",
                    {"project_id": str(project_id), "fixture_name": "site_report.txt"},
                )
            )
            document_id = UUID(str(source["document_id"]))
            run = _content(
                await client.call_tool(
                    "start_run",
                    {"project_id": str(project_id), "idempotency_key": "mcp-run-real-1"},
                )
            )
            run_id = UUID(str(run["id"]))
            executor = DeterministicStageExecutor(sessions)
            for _ in range(10):
                if not await run_worker_iteration(
                    workflow_core,
                    worker_id="real-mcp-worker",
                    execute_stage=executor,
                ):
                    break
            inspected = _content(
                await client.call_tool(
                    "inspect_run",
                    {"project_id": str(project_id), "run_id": str(run_id)},
                )
            )
            assert inspected["status"] == "review_ready"

            started = _content(
                await client.call_tool(
                    "start_edit",
                    {
                        "project_id": str(project_id),
                        "document_id": str(document_id),
                        "workflow_run_id": str(run_id),
                        "prompt": "apply the cited quantity",
                        "idempotency_key": "mcp-edit-real-1",
                    },
                )
            )
            ready = _content(
                await client.call_tool(
                    "poll_provider_job",
                    {
                        "project_id": str(project_id),
                        "job_id": str(started["id"]),
                    },
                )
            )
            assert ready["status"] == "review_ready"
            batch_id = UUID(str(ready["review_batch_id"]))
            batch = await reviews.get_batch(batch_id, human)
            grant = await reviews.request_confirmation(batch_id, human)
            applied = _content(
                await client.call_tool(
                    "submit_review_decisions",
                    {
                        "project_id": str(project_id),
                        "batch_id": str(batch_id),
                        "idempotency_key": "mcp-decision-real-1",
                        "confirmation_token": grant.token,
                        "decisions": [
                            {"item_id": item["id"], "decision": "approve", "note": None}
                            for item in batch["items"]
                        ],
                    },
                )
            )
            assert applied["status"] == "completed"
            pending = _content(
                await client.call_tool(
                    "list_pending_reviews", {"project_id": str(project_id)}
                )
            )
            assert pending == {"items": [], "total": 0}
            audit = _content(
                await client.call_tool(
                    "inspect_audit", {"project_id": str(project_id), "limit": 100}
                )
            )
            assert any(
                item["event_type"] == "review.decisions_applied"
                and item["command_id"]
                and item["decision_id"]
                for item in audit["items"]
            )
            exported = _content(
                await client.call_tool(
                    "export",
                    {
                        "project_id": str(project_id),
                        "document_id": str(document_id),
                        "format": "docx",
                    },
                )
            )
            assert exported["status"] == "completed"

        async with sessions() as session:
            citation = (
                await session.execute(
                    select(tables.citations).select_from(
                        tables.citations.join(tables.facts)
                    ).where(tables.facts.c.workflow_run_id == run_id)
                )
            ).mappings().one()
        assert citation["quoted_text"] == "Installed quantity: 420 m"
    finally:
        async with engine.begin() as connection:
            aggregate_ids = list(
                await connection.scalars(
                    select(tables.audit_events.c.aggregate_id).where(
                        tables.audit_events.c.project_id == project_id
                    )
                )
            )
            if aggregate_ids:
                await connection.execute(
                    delete(tables.outbox).where(tables.outbox.c.aggregate_id.in_(aggregate_ids))
                )
            await connection.execute(
                delete(tables.audit_events).where(tables.audit_events.c.project_id == project_id)
            )
            await connection.execute(
                delete(tables.projects).where(tables.projects.c.id == project_id)
            )
            await connection.execute(
                delete(tables.artifact_blobs).where(
                    tables.artifact_blobs.c.id.not_in(
                        select(tables.stage_artifacts.c.artifact_blob_id)
                    )
                )
            )
        await engine.dispose()
