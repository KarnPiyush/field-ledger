from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fieldledger.agent.stages import DeterministicStageExecutor
from fieldledger.config import Settings
from fieldledger.domain.models import StageName
from fieldledger.persistence.tables import (
    artifact_blobs,
    audit_events,
    document_versions,
    documents,
    ingestion_outcomes,
    outbox,
    projects,
    source_chunks,
    stage_artifacts,
    stage_attempts,
    stage_dependencies,
    workflow_runs,
)
from fieldledger.services.ingestion_service import IngestionService, IngestSource
from fieldledger.services.invalidation_service import InvalidationService
from fieldledger.services.workflow_service import WorkflowService
from fieldledger.worker import run_worker_iteration
from fixtures.generate import fixture_bytes
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def ingestion_context() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker, UUID, WorkflowService, IngestionService]
]:
    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id = uuid4()
    async with engine.begin() as connection:
        server_version = int(await connection.scalar(text("SHOW server_version_num")) or 0)
        assert 170000 <= server_version < 180000
        await connection.execute(
            insert(projects).values(id=project_id, name=f"Ingestion test {project_id}")
        )
    workflow = WorkflowService(sessions)
    ingestion = IngestionService(sessions, workflow=workflow, max_bytes=2_000_000)
    try:
        yield engine, sessions, project_id, workflow, ingestion
    finally:
        async with engine.begin() as connection:
            run_ids = set(
                (
                    await connection.execute(
                        select(workflow_runs.c.id).where(
                            workflow_runs.c.project_id == project_id
                        )
                    )
                ).scalars()
            )
            if run_ids:
                await connection.execute(
                    delete(outbox).where(outbox.c.aggregate_id.in_(run_ids))
                )
            await connection.execute(
                delete(audit_events).where(audit_events.c.project_id == project_id)
            )
            await connection.execute(delete(projects).where(projects.c.id == project_id))
            if run_ids:
                await connection.execute(
                    delete(artifact_blobs).where(
                        artifact_blobs.c.id.not_in(select(stage_artifacts.c.artifact_blob_id))
                    )
                )
        await engine.dispose()


def _source(name: str, content: bytes) -> IngestSource:
    media_types = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }
    return IngestSource(name=name, media_type=media_types[Path(name).suffix], content=content)


async def test_pile_commits_good_files_and_records_each_bad_file_independently(
    ingestion_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, _workflow, ingestion = ingestion_context
    fixtures = fixture_bytes()

    result = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source("contract.pdf", fixtures["contract.pdf"]),
            IngestSource(name="broken.pdf", media_type="application/pdf", content=b"broken"),
            IngestSource(name="legacy.doc", media_type="application/msword", content=b"legacy"),
        ),
    )

    assert [item.status for item in result.files] == ["parsed", "failed", "unsupported"]
    assert result.run_id is not None
    async with sessions() as session:
        assert await session.scalar(
            select(func.count()).select_from(document_versions).join(documents).where(
                documents.c.project_id == project_id
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(source_chunks).join(document_versions).join(
                documents
            ).where(documents.c.project_id == project_id)
        ) == 2
        statuses = (
            await session.execute(
                select(ingestion_outcomes.c.status)
                .where(ingestion_outcomes.c.project_id == project_id)
                .order_by(ingestion_outcomes.c.created_at, ingestion_outcomes.c.source_name)
            )
        ).scalars().all()
    assert sorted(statuses) == ["failed", "parsed", "unsupported"]


async def test_duplicate_after_rename_creates_no_version_or_run_and_change_is_immutable(
    ingestion_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, _workflow, ingestion = ingestion_context
    original_bytes = fixture_bytes()["site_report.txt"]
    first = await ingestion.ingest_pile(
        project_id=project_id, sources=(_source("site_report.txt", original_bytes),)
    )
    renamed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            IngestSource(
                name="renamed.txt",
                media_type="text/plain",
                content=original_bytes,
                previous_name="site_report.txt",
            ),
        ),
    )
    mismatched_duplicate = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            IngestSource(
                name="renamed-again.txt",
                media_type="application/pdf",
                content=original_bytes,
            ),
        ),
    )
    changed_bytes = original_bytes.replace(b"420 m", b"421 m")
    changed = await ingestion.ingest_pile(
        project_id=project_id, sources=(_source("site_report.txt", changed_bytes),)
    )

    assert first.run_id is not None
    assert renamed.run_id is None and renamed.files[0].status == "duplicate"
    assert mismatched_duplicate.run_id is None
    assert mismatched_duplicate.files[0].status == "unsupported"
    assert changed.run_id is not None and changed.files[0].version_number == 2
    assert changed.files[0].previous_version_id == first.files[0].document_version_id
    async with sessions() as session:
        versions = (
            await session.execute(
                select(
                    document_versions.c.version_number,
                    document_versions.c.content_hash,
                    document_versions.c.raw_content,
                )
                .join(documents)
                .where(documents.c.project_id == project_id)
                .order_by(document_versions.c.version_number)
            )
        ).all()
        run_count = await session.scalar(
            select(func.count()).select_from(workflow_runs).where(
                workflow_runs.c.project_id == project_id
            )
        )
    assert [row.version_number for row in versions] == [1, 2]
    assert [row.raw_content for row in versions] == [original_bytes, changed_bytes]
    assert versions[0].content_hash != versions[1].content_hash
    assert run_count == 2


@pytest.mark.parametrize(
    ("source", "expected_status"),
    [
        (IngestSource(name="../escape.txt", media_type="text/plain", content=b"x"), "failed"),
        (IngestSource(name="empty.txt", media_type="text/plain", content=b""), "failed"),
        (
            IngestSource(name="mismatch.txt", media_type="application/pdf", content=b"text"),
            "unsupported",
        ),
        (
            IngestSource(
                name="large.txt", media_type="text/plain", content=b"x" * 2_000_001
            ),
            "failed",
        ),
    ],
)
async def test_invalid_source_is_rejected_per_file(
    ingestion_context, source: IngestSource, expected_status: str
) -> None:  # type: ignore[no-untyped-def]
    _engine, _sessions, project_id, _workflow, ingestion = ingestion_context

    result = await ingestion.ingest_pile(project_id=project_id, sources=(source,))

    assert result.run_id is None
    assert result.files[0].status == expected_status
    assert result.files[0].problem is not None


async def test_stage_adapter_commits_canonical_blocks_and_granular_dependencies(
    ingestion_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = ingestion_context
    fixtures = fixture_bytes()
    await ingestion.store_rule_set(project_id=project_id, raw_content=fixtures["rules.json"])
    ingested = await ingestion.ingest_pile(
        project_id=project_id,
        sources=tuple(
            _source(name, fixtures[name])
            for name in ("contract.pdf", "change_order.docx", "site_report.txt", "invoice.md")
        ),
    )
    assert ingested.run_id is not None
    executor = DeterministicStageExecutor(sessions)

    for expected_stage in StageName:
        assert await run_worker_iteration(
            workflow, worker_id="task-3-test", execute_stage=executor
        )
        snapshot = await workflow.snapshot(ingested.run_id)
        assert any(
            item.stage is expected_stage and item.status.value == "completed"
            for item in snapshot.attempts
        )

    async with sessions() as session:
        rows = (
            await session.execute(
                select(artifact_blobs.c.content, artifact_blobs.c.artifact_hash)
                .select_from(
                    artifact_blobs.join(stage_artifacts).join(stage_attempts)
                )
                .where(stage_attempts.c.workflow_run_id == ingested.run_id)
            )
        ).all()
        dependencies = (
            await session.execute(
                select(stage_dependencies.c.dependency_key)
                .select_from(stage_dependencies.join(stage_artifacts))
                .where(stage_artifacts.c.workflow_run_id == ingested.run_id)
            )
        ).scalars().all()
    for content, artifact_hash in rows:
        assert json.dumps(
            json.loads(content), sort_keys=True, separators=(",", ":")
        ).encode() == content
        assert artifact_hash.startswith("sha256:")
    assert any(key.startswith("source-chunk:") for key in dependencies)
    assert any(key.startswith("artifact:") for key in dependencies)


async def test_changed_source_invalidates_only_dependency_closure_from_extract(
    ingestion_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = ingestion_context
    original = fixture_bytes()["site_report.txt"]
    first = await ingestion.ingest_pile(
        project_id=project_id, sources=(_source("site_report.txt", original),)
    )
    assert first.run_id is not None
    executor = DeterministicStageExecutor(sessions)
    for _stage in StageName:
        await run_worker_iteration(workflow, worker_id="task-3-test", execute_stage=executor)

    async with sessions() as session:
        old_run = dict(
            (
                await session.execute(
                    select(workflow_runs).where(workflow_runs.c.id == first.run_id)
                )
            ).mappings().one()
        )
        old_attempts = tuple(
            dict(row)
            for row in (
                await session.execute(
                    select(stage_attempts)
                    .where(stage_attempts.c.workflow_run_id == first.run_id)
                    .order_by(stage_attempts.c.stage, stage_attempts.c.attempt_number)
                )
            ).mappings()
        )
        old_artifacts = tuple(
            (
                row.kind,
                row.artifact_hash,
                row.content,
            )
            for row in (
                await session.execute(
                    select(
                        stage_artifacts.c.kind,
                        artifact_blobs.c.artifact_hash,
                        artifact_blobs.c.content,
                    )
                    .select_from(stage_artifacts.join(artifact_blobs))
                    .where(stage_artifacts.c.workflow_run_id == first.run_id)
                    .order_by(stage_artifacts.c.kind, artifact_blobs.c.artifact_hash)
                )
            )
        )

    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(_source("site_report.txt", original.replace(b"420 m", b"421 m")),),
    )
    assert changed.run_id is not None
    plan = await InvalidationService(sessions).invalidate_ingestion(
        changed.files[0], successor_run_id=changed.run_id
    )

    assert plan is not None
    assert plan.run_ids == (changed.run_id,)
    assert plan.earliest_stage is StageName.EXTRACT
    async with sessions() as session:
        new_run = dict(
            (
                await session.execute(
                    select(workflow_runs).where(workflow_runs.c.id == first.run_id)
                )
            ).mappings().one()
        )
        new_attempts = tuple(
            dict(row)
            for row in (
                await session.execute(
                    select(stage_attempts)
                    .where(stage_attempts.c.workflow_run_id == first.run_id)
                    .order_by(stage_attempts.c.stage, stage_attempts.c.attempt_number)
                )
            ).mappings()
        )
        new_artifacts = tuple(
            (
                row.kind,
                row.artifact_hash,
                row.content,
            )
            for row in (
                await session.execute(
                    select(
                        stage_artifacts.c.kind,
                        artifact_blobs.c.artifact_hash,
                        artifact_blobs.c.content,
                    )
                    .select_from(stage_artifacts.join(artifact_blobs))
                    .where(stage_artifacts.c.workflow_run_id == first.run_id)
                    .order_by(stage_artifacts.c.kind, artifact_blobs.c.artifact_hash)
                )
            )
        )
        entries = (
            await session.execute(
                text(
                    "SELECT workflow_run_id, document_version_id, earliest_stage, "
                    "affected_block_key FROM run_invalidation_entries "
                    "WHERE workflow_run_id = :run_id"
                ),
                {"run_id": changed.run_id},
            )
        ).all()
    assert (new_run, new_attempts, new_artifacts) == (
        old_run,
        old_attempts,
        old_artifacts,
    )
    assert entries
    assert {row.workflow_run_id for row in entries} == {changed.run_id}
    assert {row.document_version_id for row in entries} == {
        changed.files[0].document_version_id
    }
    assert {row.earliest_stage for row in entries} == {StageName.EXTRACT.value}
    assert {row.affected_block_key for row in entries} == set(
        plan.affected_block_keys
    )
