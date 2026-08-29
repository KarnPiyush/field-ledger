from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
import pytest
from fieldledger.agent.stages import DeterministicStageExecutor
from fieldledger.api.dependencies import ServiceContainer
from fieldledger.config import Settings
from fieldledger.main import create_app
from fieldledger.persistence import tables
from fieldledger.providers.deterministic import DeterministicDocumentProvider
from fieldledger.security import (
    ActorSource,
    DemoIdentity,
    GlobalRole,
    ProjectAuthorizer,
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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


async def test_real_rest_source_then_idempotent_run_has_manifest_and_citation() -> None:
    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workflow = WorkflowService(sessions)
    ingestion = IngestionService(sessions, workflow=workflow)
    authorization = ProjectAuthorizer(sessions)
    provider = DeterministicDocumentProvider()
    security = SecurityManager(
        signing_secret="placeholder-real-rest-signing",
        issuer="fieldledger-real-rest-test",
        audience="fieldledger-real-rest-client",
        local_demo_enabled=True,
        demo_bootstrap_secret="placeholder-real-rest-bootstrap",
        demo_identities={
            "reviewer": DemoIdentity(
                actor_id="real-rest-reviewer",
                source=ActorSource.UI,
                global_role=GlobalRole.USER,
            )
        },
    )
    dependencies = DependencyService(sessions, policy_content_hash="sha256:rest-policy-v1")
    services = ServiceContainer(
        projects=ProjectService(sessions, authorization),
        ingestion=SourceService(ingestion, authorization),
        workflow=RunService(sessions, workflow, authorization),
        reviews=ReviewService(
            sessions,
            provider=provider,
            security=security,
            authorization=authorization,
            dependencies=dependencies,
        ),
        audit=AuditService(sessions, authorization),
        providers=ProviderService(
            sessions,
            provider,
            authorization=authorization,
            dependencies=dependencies,
        ),
        exports=ExportService(sessions, provider, authorization=authorization),
    )
    project_id: UUID | None = None
    app = create_app(services=services, security=security)
    now = datetime.now(UTC)
    token = security.mint_demo_session(
        identity="reviewer",
        bootstrap_secret="placeholder-real-rest-bootstrap",
        now=now,
        lifetime=timedelta(hours=1),
    )
    headers = {"Authorization": f"Bearer {token}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://fieldledger.test"
        ) as client:
            created = await client.post(
                "/api/v1/projects",
                headers=headers,
                json={"name": "Real REST workflow"},
            )
            assert created.status_code == 201
            project_id = UUID(created.json()["id"])
            source = await client.post(
                f"/api/v1/projects/{project_id}/sources",
                headers=headers,
                json={
                    "name": "site-report.txt",
                    "media_type": "text/plain",
                    "content_base64": base64.b64encode(
                        b"Document type: Site Report\nInstalled quantity: 420 m\n"
                    ).decode(),
                },
            )
            assert source.status_code == 201
            run_headers = {**headers, "Idempotency-Key": "rest-run-1"}
            first = await client.post(
                f"/api/v1/projects/{project_id}/runs", headers=run_headers, json={}
            )
            replay = await client.post(
                f"/api/v1/projects/{project_id}/runs", headers=run_headers, json={}
            )
            assert first.status_code == replay.status_code == 202
            assert replay.json()["id"] == first.json()["id"]
            run_id = UUID(first.json()["id"])

        async with sessions() as session:
            manifest = (
                await session.execute(
                    select(tables.run_input_manifest).where(
                        tables.run_input_manifest.c.workflow_run_id == run_id
                    )
                )
            ).mappings().all()
        assert manifest and {row["kind"] for row in manifest} == {"source_version"}
        executor = DeterministicStageExecutor(sessions)
        for _ in range(10):
            if not await run_worker_iteration(
                workflow, worker_id="real-rest-worker", execute_stage=executor
            ):
                break
        async with sessions() as session:
            cited = (
                await session.execute(
                    select(tables.facts.c.fact_key, tables.citations.c.quoted_text)
                    .select_from(tables.facts.join(tables.citations))
                    .where(tables.facts.c.workflow_run_id == run_id)
                )
            ).all()
        assert cited == [("installed_quantity", "Installed quantity: 420 m")]
    finally:
        if project_id is not None:
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
                        delete(tables.outbox).where(
                            tables.outbox.c.aggregate_id.in_(aggregate_ids)
                        )
                    )
                await connection.execute(
                    delete(tables.audit_events).where(
                        tables.audit_events.c.project_id == project_id
                    )
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
