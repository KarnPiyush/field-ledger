from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fieldledger.config import Settings
from fieldledger.domain.models import ReviewDecision, ReviewDecisionKind
from fieldledger.domain.review import DecisionSubmission
from fieldledger.persistence.tables import (
    audit_events,
    document_versions,
    documents,
    outbox,
    project_memberships,
    projects,
    rule_sets,
    run_input_manifest,
    workflow_runs,
)
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
from fieldledger.services.provider_service import ProviderService
from fieldledger.services.review_service import ReviewService
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 23, 14, 0, tzinfo=UTC)


async def test_deterministic_async_edit_review_repeat_complete_export_audit() -> None:
    engine = create_async_engine(Settings.from_environment().database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id, document_id, source_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    target_version_id, source_version_id = uuid4(), uuid4()
    actor = Actor(
        actor_id="lifecycle-reviewer",
        source=ActorSource.UI,
        global_role=GlobalRole.USER,
    )
    async with engine.begin() as connection:
        await connection.execute(insert(projects).values(id=project_id, name="Lifecycle"))
        await connection.execute(
            insert(project_memberships).values(
                project_id=project_id,
                actor_id=actor.actor_id,
                role=ProjectRole.REVIEWER.value,
                granted_by="server-lifecycle-fixture",
            )
        )
        await connection.execute(
            insert(documents),
            [
                {
                    "id": document_id,
                    "project_id": project_id,
                    "name": "target.docx",
                    "media_type": (
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                    "fingerprint": "sha256:lifecycle-target",
                },
                {
                    "id": source_id,
                    "project_id": project_id,
                    "name": "source.txt",
                    "media_type": "text/plain",
                    "fingerprint": "sha256:lifecycle-source",
                },
            ],
        )
        await connection.execute(
            insert(document_versions),
            [
                {
                    "id": target_version_id,
                    "document_id": document_id,
                    "version_number": 1,
                    "content_hash": "sha256:lifecycle-target-v1",
                    "storage_uri": "postgresql://lifecycle/target",
                    "byte_size": 6,
                    "raw_content": b"target",
                },
                {
                    "id": source_version_id,
                    "document_id": source_id,
                    "version_number": 1,
                    "content_hash": "sha256:lifecycle-source-v1",
                    "storage_uri": "postgresql://lifecycle/source",
                    "byte_size": 6,
                    "raw_content": b"source",
                },
            ],
        )
        await connection.execute(
            insert(rule_sets).values(
                project_id=project_id,
                version_number=1,
                version="v1",
                content_hash="sha256:lifecycle-rules-v1",
                raw_content=b"{}",
                rules=[],
            )
        )
        await connection.execute(
            insert(workflow_runs).values(
                id=run_id,
                project_id=project_id,
                status="review_ready",
                input_hash="sha256:lifecycle-run",
            )
        )
        await connection.execute(
            insert(run_input_manifest).values(
                workflow_run_id=run_id,
                kind="source_version",
                object_id=target_version_id,
                content_hash="sha256:lifecycle-target-v1",
            )
        )

    security = SecurityManager(
        signing_secret="placeholder-lifecycle-signing",
        issuer="fieldledger-test",
        audience="fieldledger-test-client",
        local_demo_enabled=False,
    )
    authorization = ProjectAuthorizer(sessions)
    dependencies = DependencyService(sessions, policy_content_hash="sha256:policy-v1")
    provider = DeterministicDocumentProvider()
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
    exports = ExportService(sessions, provider, authorization=authorization)

    try:
        started = await providers.start_edit(
            document_id=document_id,
            workflow_run_id=run_id,
            prompt="apply approved commercial updates",
            idempotency_key="lifecycle-edit-1",
            actor=actor,
        )
        assert started["status"] == "processing"

        ready = await providers.poll(started["id"], actor)
        assert ready["status"] == "review_ready"
        first_batch_id = ready["review_batch_id"]
        first_batch = await reviews.get_batch(first_batch_id, actor)
        assert len(first_batch["items"]) == 2

        token = (
            await reviews.request_confirmation(
                first_batch_id, actor, now=NOW, lifetime=timedelta(minutes=5)
            )
        ).token
        first = await reviews.submit_decisions(
            first_batch_id,
            DecisionSubmission(
                idempotency_key="lifecycle-decision-1",
                confirmation_token=token,
                decisions=(
                    ReviewDecision(
                        item_id=first_batch["items"][0]["id"],
                        decision=ReviewDecisionKind.APPROVE,
                    ),
                    ReviewDecision(
                        item_id=first_batch["items"][1]["id"],
                        decision=ReviewDecisionKind.REJECT,
                        note="revise the supporting explanation",
                    ),
                ),
            ),
            actor,
            now=NOW,
        )
        assert first.status == "review_ready"
        assert first.next_batch_id is not None

        next_batch = await reviews.get_batch(first.next_batch_id, actor)
        next_token = (
            await reviews.request_confirmation(
                first.next_batch_id,
                actor,
                now=NOW + timedelta(seconds=1),
                lifetime=timedelta(minutes=5),
            )
        ).token
        completed = await reviews.submit_decisions(
            first.next_batch_id,
            DecisionSubmission(
                idempotency_key="lifecycle-decision-2",
                confirmation_token=next_token,
                decisions=tuple(
                    ReviewDecision(
                        item_id=item["id"], decision=ReviewDecisionKind.APPROVE
                    )
                    for item in next_batch["items"]
                ),
            ),
            actor,
            now=NOW + timedelta(seconds=1),
        )
        assert completed.status == "completed"

        exported = await exports.export_document(document_id, format="docx", actor=actor)
        assert exported["status"] == "completed"
        async with sessions() as session:
            event_types = set(
                await session.scalars(
                    select(audit_events.c.event_type).where(
                        audit_events.c.project_id == project_id
                    )
                )
            )
        assert "provider.edit_started" in event_types
        assert "provider.review_ready" in event_types
        assert "review.decisions_applied" in event_types
    finally:
        async with engine.begin() as connection:
            aggregate_ids = list(
                await connection.scalars(
                    select(audit_events.c.aggregate_id).where(
                        audit_events.c.project_id == project_id
                    )
                )
            )
            if aggregate_ids:
                await connection.execute(
                    delete(outbox).where(outbox.c.aggregate_id.in_(aggregate_ids))
                )
            await connection.execute(
                delete(audit_events).where(audit_events.c.project_id == project_id)
            )
            await connection.execute(delete(projects).where(projects.c.id == project_id))
        await engine.dispose()
