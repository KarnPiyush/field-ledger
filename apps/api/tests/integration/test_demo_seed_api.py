from __future__ import annotations

import asyncio
from importlib import import_module
from uuid import UUID

import httpx
import pytest
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
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


async def test_demo_seed_rest_contract_persists_authoritative_hydration_state() -> None:
    try:
        demo_module = import_module("fieldledger.services.demo_service")
    except ModuleNotFoundError:
        pytest.fail("DemoSeedService must implement the seed contract")
    demo_service_type = getattr(demo_module, "DemoSeedService", None)
    assert demo_service_type is not None, "DemoSeedService must implement the seed contract"

    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    workflow = WorkflowService(sessions)
    ingestion_core = IngestionService(sessions, workflow=workflow)
    authorization = ProjectAuthorizer(sessions)
    dependencies = DependencyService(sessions, policy_content_hash="sha256:demo-policy-v1")
    provider = DeterministicDocumentProvider()
    security = SecurityManager(
        signing_secret="placeholder-demo-seed-signing",
        issuer="fieldledger-demo-seed-test",
        audience="fieldledger-demo-seed-client",
        local_demo_enabled=True,
        demo_bootstrap_secret="placeholder-demo-seed-bootstrap",
        demo_identities={
            "reviewer": DemoIdentity(
                actor_id="demo-seed-reviewer",
                source=ActorSource.UI,
                global_role=GlobalRole.USER,
            )
        },
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
        workflow=RunService(sessions, workflow, authorization),
        reviews=reviews,
        audit=AuditService(sessions, authorization),
        providers=providers,
        exports=ExportService(sessions, provider, authorization=authorization),
    )
    object.__setattr__(
        services,
        "demo",
        demo_service_type(
            sessions,
            enabled=True,
            provider=provider,
            workflow=workflow,
            ingestion=ingestion_core,
            providers=providers,
        ),
    )
    project_id: UUID | None = None
    app = create_app(services=services, security=security)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://fieldledger.test",
        ) as client:
            session_response = await client.post(
                "/api/v1/auth/demo-session",
                headers={
                    "X-FieldLedger-Demo-Bootstrap": "placeholder-demo-seed-bootstrap"
                },
                json={"identity": "reviewer"},
            )
            assert session_response.status_code == 201
            token = session_response.json()["access_token"]
            headers = {
                "Authorization": f"Bearer {token}",
                "Idempotency-Key": "demo-seed-api-1",
            }
            first, replay = await asyncio.gather(
                client.post("/api/v1/demo/seed", headers=headers, json={}),
                client.post("/api/v1/demo/seed", headers=headers, json={}),
            )
            assert first.status_code == replay.status_code == 200
            responses = sorted(
                (first.json(), replay.json()), key=lambda item: item["replayed"]
            )
            body, replay_body = responses
            assert replay_body == {**body, "replayed": True}
            project_id = UUID(body["project_id"])
            UUID(body["workflow_run_id"])
            batch_id = UUID(body["review_batch_id"])
            UUID(body["document_id"])
            assert body["status"] == "review_ready"

            listed = await client.get(
                f"/api/v1/projects/{project_id}/reviews", headers=headers
            )
            batch = await client.get(f"/api/v1/reviews/{batch_id}", headers=headers)
            audit = await client.get(
                f"/api/v1/projects/{project_id}/audit", headers=headers
            )
            assert listed.status_code == batch.status_code == audit.status_code == 200
            assert listed.json()["items"][0]["id"] == str(batch_id)
            assert batch.json()["dependency_digest"]
            items = batch.json()["items"]
            assert len(items) == 5
            assert {item["title"] for item in items} == {
                "Contract value",
                "Feeder allowance",
                "Completion date",
                "Installed quantity",
                "Invoice amount",
            }
            assert all(item["evidence"] and item["citation"] for item in items)
            for item in items:
                citation = item["citation"]
                assert UUID(citation["document_version_id"])
                assert UUID(citation["source_chunk_id"])
                assert citation["quoted_text"]
            assert audit.json()["items"]
            assert sum(
                item["event_type"] == "demo.seeded" for item in audit.json()["items"]
            ) == 1

            reviewed = await dependencies.reviewed_vector(batch_id)
            current = await dependencies.current_vector(batch_id)
            assert reviewed.digest == current.digest == batch.json()["dependency_digest"]

        async with sessions() as db:
            assert await db.scalar(
                select(tables.projects.c.name).where(tables.projects.c.id == project_id)
            ) == "Riverside Medical Center"
            assert await db.scalar(
                select(tables.project_memberships.c.id).where(
                    tables.project_memberships.c.project_id == project_id,
                    tables.project_memberships.c.actor_id == "demo-seed-reviewer",
                )
            )
            assert await db.scalar(
                select(tables.documents.c.name).where(
                    tables.documents.c.id == UUID(body["document_id"])
                )
            ) == "change-order-014.docx"
            assert await db.scalar(
                select(tables.review_batches.c.id).where(
                    tables.review_batches.c.project_id == project_id,
                    tables.review_batches.c.status == "open",
                )
            ) == batch_id
            for item in items:
                citation = item["citation"]
                resolved = await db.scalar(
                    select(tables.source_chunks.c.id).where(
                        tables.source_chunks.c.id == UUID(citation["source_chunk_id"]),
                        tables.source_chunks.c.document_version_id
                        == UUID(citation["document_version_id"]),
                        tables.source_chunks.c.content.contains(citation["quoted_text"]),
                    )
                )
                assert resolved
            assert await db.scalar(
                select(tables.source_chunks.c.id)
                .select_from(
                    tables.source_chunks.join(tables.document_versions).join(tables.documents)
                )
                .where(tables.documents.c.project_id == project_id)
            )
            assert await db.scalar(
                select(tables.stage_artifacts.c.id)
                .select_from(tables.stage_artifacts.join(tables.workflow_runs))
                .where(tables.workflow_runs.c.project_id == project_id)
            )
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
