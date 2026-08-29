from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from fieldledger.agent.stages import DeterministicStageExecutor
from fieldledger.config import Settings
from fieldledger.domain.models import ArtifactKind, DependencyKind, StageName, StageResult
from fieldledger.persistence import tables
from fieldledger.persistence.repositories import WorkflowRepository
from fieldledger.security import Actor, ActorSource, GlobalRole, ProjectAuthorizer, ProjectRole
from fieldledger.services.ingestion_service import IngestionService, IngestSource
from fieldledger.services.invalidation_service import InvalidationService
from fieldledger.services.project_service import RunService, SourceService
from fieldledger.services.workflow_service import WorkflowService
from fieldledger.watcher import InboxWatcher
from fieldledger.worker import run_worker_iteration
from fixtures.generate import fixture_bytes
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def review_context() -> AsyncIterator[
    tuple[AsyncEngine, async_sessionmaker, UUID, WorkflowService, IngestionService]
]:
    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    project_id = uuid4()
    async with engine.begin() as connection:
        server_version = int(await connection.scalar(text("SHOW server_version_num")) or 0)
        assert 170000 <= server_version < 180000
        await connection.execute(
            insert(tables.projects).values(id=project_id, name=f"Review fixes {project_id}")
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
                        select(tables.workflow_runs.c.id).where(
                            tables.workflow_runs.c.project_id == project_id
                        )
                    )
                ).scalars()
            )
            if run_ids:
                await connection.execute(
                    delete(tables.outbox).where(tables.outbox.c.aggregate_id.in_(run_ids))
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


def _source(name: str, content: bytes) -> IngestSource:
    media_types = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }
    return IngestSource(name=name, media_type=media_types[Path(name).suffix], content=content)


async def _drain(workflow: WorkflowService, executor: DeterministicStageExecutor) -> None:
    for _ in range(100):
        if not await run_worker_iteration(
            workflow, worker_id="task-3-review", execute_stage=executor
        ):
            return
    raise AssertionError("workflow queue did not drain")


async def test_rest_services_start_one_manifest_bearing_cited_run_per_key(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    actor = Actor(
        actor_id="manifest-reviewer",
        source=ActorSource.UI,
        global_role=GlobalRole.USER,
    )
    async with sessions.begin() as session:
        await session.execute(
            insert(tables.project_memberships).values(
                project_id=project_id,
                actor_id=actor.actor_id,
                role=ProjectRole.REVIEWER.value,
                granted_by="test",
            )
        )
    authorization = ProjectAuthorizer(sessions)
    sources = SourceService(ingestion, authorization)
    runs = RunService(sessions, workflow, authorization)

    await sources.add_source(
        project_id,
        name="site-report.txt",
        media_type="text/plain",
        content=b"Document type: Site Report\nInstalled quantity: 420 m\n",
        actor=actor,
    )
    first = await runs.start_run(
        project_id, actor=actor, idempotency_key="manifest-run-1"
    )
    replay = await runs.start_run(
        project_id, actor=actor, idempotency_key="manifest-run-1"
    )

    assert replay["id"] == first["id"]
    run_id = UUID(str(first["id"]))
    async with sessions() as session:
        manifest = (
            await session.execute(
                select(tables.run_input_manifest).where(
                    tables.run_input_manifest.c.workflow_run_id == run_id
                )
            )
        ).mappings().all()
    assert {row["kind"] for row in manifest} == {DependencyKind.SOURCE_VERSION.value}

    await _drain(workflow, DeterministicStageExecutor(sessions))
    async with sessions() as session:
        cited = (
            await session.execute(
                select(tables.facts.c.fact_key, tables.citations.c.quoted_text)
                .select_from(tables.facts.join(tables.citations))
                .where(tables.facts.c.workflow_run_id == run_id)
            )
        ).all()
    assert cited == [("installed_quantity", "Installed quantity: 420 m")]


async def test_identical_bytes_at_distinct_paths_keep_distinct_document_lineages(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, _sessions, project_id, _workflow, ingestion = review_context
    shared = b"Document type: Site Report\nInstalled quantity: 420 m\n"

    first = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source("north.txt", shared),
            _source("south.txt", shared),
        ),
    )

    assert first.files[0].document_id != first.files[1].document_id
    assert {item.status for item in first.files} == {"parsed"}

    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source("north.txt", shared.replace(b"420 m", b"421 m")),
            _source("south.txt", shared.replace(b"420 m", b"422 m")),
        ),
    )
    assert changed.files[0].document_id == first.files[0].document_id
    assert changed.files[1].document_id == first.files[1].document_id


async def test_new_unrelated_excluded_claim_reuses_other_fact_drafts_byte_for_byte(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    first = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source(
                "contract.txt",
                b"Document type: Contract\nContract value: USD 1000000\n",
            ),
            _source(
                "invoice.md",
                b"Document type: Invoice\nInvoice amount: USD 122500\n",
            ),
        ),
    )
    assert first.run_id is not None
    await _drain(workflow, DeterministicStageExecutor(sessions))

    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source(
                "contract.txt",
                (
                    b"Document type: Contract\nContract value: USD 1000000\n"
                    b"Unsupported claim: savings_projection = USD 20000\n"
                ),
            ),
        ),
    )
    assert changed.run_id is not None
    await _drain(workflow, DeterministicStageExecutor(sessions))

    old = {item["block_key"]: item for item in await _artifact_payloads(sessions, first.run_id)}
    new = {
        item["block_key"]: item
        for item in await _artifact_payloads(sessions, changed.run_id)
    }
    for key in ("register:invoice_amount", "brief:invoice_amount"):
        assert new[key]["raw"] == old[key]["raw"]
        assert new[key]["hash"] == old[key]["hash"]


async def _artifact_payloads(sessions: async_sessionmaker, run_id: UUID) -> list[dict]:
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    tables.stage_artifacts.c.kind,
                    tables.artifact_blobs.c.artifact_hash,
                    tables.artifact_blobs.c.content,
                )
                .select_from(tables.stage_artifacts.join(tables.artifact_blobs))
                .where(tables.stage_artifacts.c.workflow_run_id == run_id)
            )
        ).all()
    return [
        {"kind": row.kind, "hash": row.artifact_hash, "raw": row.content, **json.loads(row.content)}
        for row in rows
        if row.kind != ArtifactKind.EXECUTION_EVIDENCE.value
    ]


async def _run_durable_state(
    sessions: async_sessionmaker, run_id: UUID
) -> tuple[dict, tuple[dict, ...], tuple[tuple[str, str, bytes], ...]]:
    async with sessions() as session:
        run = dict(
            (
                await session.execute(
                    select(tables.workflow_runs).where(tables.workflow_runs.c.id == run_id)
                )
            )
            .mappings()
            .one()
        )
        attempts = tuple(
            dict(row)
            for row in (
                await session.execute(
                    select(tables.stage_attempts)
                    .where(tables.stage_attempts.c.workflow_run_id == run_id)
                    .order_by(
                        tables.stage_attempts.c.stage,
                        tables.stage_attempts.c.attempt_number,
                    )
                )
            ).mappings()
        )
        artifacts = tuple(
            (
                row.kind,
                row.artifact_hash,
                row.content,
            )
            for row in (
                await session.execute(
                    select(
                        tables.stage_artifacts.c.kind,
                        tables.artifact_blobs.c.artifact_hash,
                        tables.artifact_blobs.c.content,
                    )
                    .select_from(tables.stage_artifacts.join(tables.artifact_blobs))
                    .where(tables.stage_artifacts.c.workflow_run_id == run_id)
                    .order_by(
                        tables.stage_artifacts.c.kind,
                        tables.artifact_blobs.c.artifact_hash,
                    )
                )
            )
        )
    return run, attempts, artifacts


class _BarrierWorkflow(WorkflowService):
    def __init__(self, sessions: async_sessionmaker, *, parties: int = 2) -> None:
        super().__init__(sessions)
        self.parties = parties
        self.arrived = 0
        self.ready = asyncio.Event()

    async def _barrier(self) -> None:
        self.arrived += 1
        if self.arrived == self.parties:
            self.ready.set()
        await asyncio.wait_for(self.ready.wait(), timeout=5)

    async def create_run(self, **kwargs):  # type: ignore[no-untyped-def]
        await self._barrier()
        return await super().create_run(**kwargs)

    async def create_incremental_run(self, **kwargs):  # type: ignore[no-untyped-def]
        await self._barrier()
        return await super().create_incremental_run(**kwargs)


async def test_run_manifest_pins_claimed_stages_and_successors_use_new_snapshots(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    fixtures = fixture_bytes()
    rule_one = await ingestion.store_rule_set(
        project_id=project_id, raw_content=fixtures["rules.json"]
    )
    first = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(_source("site_report.txt", fixtures["site_report.txt"]),),
    )
    assert rule_one.status == "stored" and rule_one.run_id is None
    assert first.run_id is not None
    executor = DeterministicStageExecutor(sessions)
    assert await run_worker_iteration(workflow, worker_id="pin-test", execute_stage=executor)
    claimed_extract = await workflow.claim_stage(worker_id="pin-test")
    assert claimed_extract is not None and claimed_extract.run_id == first.run_id
    assert claimed_extract.stage is StageName.EXTRACT

    rule_two_bytes = fixtures["rules.json"].replace(b'"expected":400', b'"expected":425')
    rule_two = await ingestion.store_rule_set(project_id=project_id, raw_content=rule_two_bytes)
    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source(
                "site_report.txt",
                fixtures["site_report.txt"].replace(b"420 m", b"421 m"),
            ),
        ),
    )
    assert rule_two.status == "stored" and rule_two.run_id is not None
    assert changed.run_id is not None

    execution = await executor(claimed_extract)
    await workflow.commit_stage(
        claimed_extract,
        execution.result,
        artifacts=execution.artifacts,
        dependencies=execution.dependencies,
    )
    await _drain(workflow, executor)

    manifest = tables.run_input_manifest
    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    manifest.c.workflow_run_id,
                    manifest.c.kind,
                    manifest.c.object_id,
                    manifest.c.content_hash,
                )
                .where(
                    manifest.c.workflow_run_id.in_((first.run_id, rule_two.run_id, changed.run_id))
                )
                .order_by(manifest.c.workflow_run_id, manifest.c.kind, manifest.c.object_id)
            )
        ).all()
        successors = (
            await session.execute(
                select(
                    tables.workflow_runs.c.id,
                    tables.workflow_runs.c.supersedes_run_id,
                ).where(tables.workflow_runs.c.id.in_((rule_two.run_id, changed.run_id)))
            )
        ).all()
    by_run = {
        run_id: {
            (kind, object_id, content_hash)
            for observed_run_id, kind, object_id, content_hash in rows
            if observed_run_id == run_id
        }
        for run_id in (first.run_id, rule_two.run_id, changed.run_id)
    }
    assert all(len(entries) == 2 for entries in by_run.values())
    assert (
        DependencyKind.SOURCE_VERSION.value,
        first.files[0].document_version_id,
        first.files[0].content_hash,
    ) in by_run[first.run_id]
    assert (
        DependencyKind.RULE_SET_VERSION.value,
        rule_one.rule_set_id,
        rule_one.content_hash,
    ) in by_run[first.run_id]
    assert (
        DependencyKind.RULE_SET_VERSION.value,
        rule_two.rule_set_id,
        rule_two.content_hash,
    ) in by_run[changed.run_id]
    assert (
        DependencyKind.SOURCE_VERSION.value,
        changed.files[0].document_version_id,
        changed.files[0].content_hash,
    ) in by_run[changed.run_id]
    assert dict(successors)[rule_two.run_id] == first.run_id
    assert dict(successors)[changed.run_id] == rule_two.run_id

    old_payloads = await _artifact_payloads(sessions, first.run_id)
    new_payloads = await _artifact_payloads(sessions, changed.run_id)
    assert any(
        item["kind"] == "fact_set" and item["payload"]["value"] == 420 for item in old_payloads
    )
    assert any(
        item["kind"] == "rule_findings"
        and item["payload"]["rule_id"] == "quantity-floor"
        and item["payload"]["expected"] == 400
        for item in old_payloads
    )
    assert any(
        item["kind"] == "fact_set" and item["payload"]["value"] == 421 for item in new_payloads
    )
    assert any(
        item["kind"] == "rule_findings"
        and item["payload"]["rule_id"] == "quantity-floor"
        and item["payload"]["expected"] == 425
        for item in new_payloads
    )


async def test_live_predecessor_blocks_successor_until_reusable_snapshot_commits(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    fixtures = fixture_bytes()
    await ingestion.store_rule_set(project_id=project_id, raw_content=fixtures["rules.json"])
    first = await ingestion.ingest_pile(
        project_id=project_id,
        sources=tuple(
            _source(name, fixtures[name])
            for name in (
                "contract.pdf",
                "change_order.docx",
                "site_report.txt",
                "invoice.md",
            )
        ),
    )
    assert first.run_id is not None
    executor = DeterministicStageExecutor(sessions)
    for _ in range(4):
        assert await run_worker_iteration(workflow, worker_id="predecessor", execute_stage=executor)
    draft_claim = await workflow.claim_stage(worker_id="predecessor")
    assert draft_claim is not None
    assert draft_claim.run_id == first.run_id and draft_claim.stage is StageName.DRAFT

    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source(
                "site_report.txt",
                fixtures["site_report.txt"].replace(b"420 m", b"421 m"),
            ),
        ),
    )
    assert changed.run_id is not None

    assert await workflow.claim_stage(worker_id="concurrent-successor") is None

    draft_execution = await executor(draft_claim)
    await workflow.commit_stage(
        draft_claim,
        draft_execution.result,
        artifacts=draft_execution.artifacts,
        dependencies=draft_execution.dependencies,
    )
    await _drain(workflow, executor)

    old = {item["block_key"]: item for item in await _artifact_payloads(sessions, first.run_id)}
    new = {item["block_key"]: item for item in await _artifact_payloads(sessions, changed.run_id)}
    unaffected = {
        key
        for key in old.keys() & new.keys()
        if key != "analysis-snapshot"
        and "installed_quantity" not in key
        and "quantity-floor" not in key
    }
    async with sessions() as session:
        evidence_rows = (
            (
                await session.execute(
                    select(tables.artifact_blobs.c.content)
                    .select_from(tables.stage_artifacts.join(tables.artifact_blobs))
                    .where(tables.stage_artifacts.c.workflow_run_id == changed.run_id)
                    .where(tables.stage_artifacts.c.kind == ArtifactKind.EXECUTION_EVIDENCE.value)
                )
            )
            .scalars()
            .all()
        )
    evidence = [json.loads(content) for content in evidence_rows]
    assert evidence
    assert {item["reuse_mode"] for item in evidence} == {"predecessor_snapshot"}
    assert {item["predecessor_run_id"] for item in evidence} == {str(first.run_id)}
    executed = {key for item in evidence for key in item["executed_block_keys"]}
    reused = {key for item in evidence for key in item["reused_block_keys"]}
    assert unaffected <= reused
    assert unaffected.isdisjoint(executed)
    for key in unaffected:
        assert new[key]["raw"] == old[key]["raw"]
        assert new[key]["hash"] == old[key]["hash"]


async def test_failed_predecessor_uses_visible_fallback_instead_of_silent_reuse(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    original = fixture_bytes()["site_report.txt"]
    first = await ingestion.ingest_pile(
        project_id=project_id, sources=(_source("site.txt", original),)
    )
    assert first.run_id is not None
    claim = await workflow.claim_stage(worker_id="failed-predecessor")
    assert claim is not None and claim.run_id == first.run_id
    await workflow.commit_stage(
        claim,
        StageResult(
            status="escalated",
            artifact_hashes=(),
            next_stage=None,
            reason="visible predecessor failure",
        ),
    )
    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(_source("site.txt", original.replace(b"420 m", b"421 m")),),
    )
    assert changed.run_id is not None
    successor_claim = await workflow.claim_stage(worker_id="fallback-successor")
    assert successor_claim is not None and successor_claim.run_id == changed.run_id

    execution = await DeterministicStageExecutor(sessions)(successor_claim)
    evidence = next(
        json.loads(item.content)
        for item in execution.artifacts
        if item.kind is ArtifactKind.EXECUTION_EVIDENCE
    )
    assert evidence["reuse_mode"] == "predecessor_failed"
    assert evidence["predecessor_run_id"] == str(first.run_id)


async def test_successor_worker_persists_dependency_granular_execution_and_reuse(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    fixtures = fixture_bytes()
    await ingestion.store_rule_set(project_id=project_id, raw_content=fixtures["rules.json"])
    first = await ingestion.ingest_pile(
        project_id=project_id,
        sources=tuple(
            _source(name, fixtures[name])
            for name in ("contract.pdf", "change_order.docx", "site_report.txt", "invoice.md")
        ),
    )
    assert first.run_id is not None
    executor = DeterministicStageExecutor(sessions)
    await _drain(workflow, executor)

    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            _source(
                "site_report.txt",
                fixtures["site_report.txt"].replace(b"420 m", b"421 m"),
            ),
        ),
    )
    assert changed.run_id is not None
    assert changed.files[0].document_version_id != first.files[2].document_version_id
    async with sessions() as session:
        old_chunk_ids = set(
            (
                await session.execute(
                    select(tables.source_chunks.c.id).where(
                        tables.source_chunks.c.document_version_id
                        == first.files[2].document_version_id
                    )
                )
            ).scalars()
        )
        new_chunk_ids = set(
            (
                await session.execute(
                    select(tables.source_chunks.c.id).where(
                        tables.source_chunks.c.document_version_id
                        == changed.files[0].document_version_id
                    )
                )
            ).scalars()
        )
    assert old_chunk_ids.isdisjoint(new_chunk_ids)
    await _drain(workflow, executor)

    old = {item["block_key"]: item for item in await _artifact_payloads(sessions, first.run_id)}
    new = {item["block_key"]: item for item in await _artifact_payloads(sessions, changed.run_id)}
    unaffected = {
        key
        for key in old.keys() & new.keys()
        if key != "analysis-snapshot"
        and "installed_quantity" not in key
        and "quantity-floor" not in key
    }
    assert unaffected
    for key in unaffected:
        assert new[key]["raw"] == old[key]["raw"], key
        assert new[key]["hash"] == old[key]["hash"]

    async with sessions() as session:
        evidence_rows = (
            (
                await session.execute(
                    select(tables.artifact_blobs.c.content)
                    .select_from(tables.stage_artifacts.join(tables.artifact_blobs))
                    .where(tables.stage_artifacts.c.workflow_run_id == changed.run_id)
                    .where(tables.stage_artifacts.c.kind == ArtifactKind.EXECUTION_EVIDENCE.value)
                )
            )
            .scalars()
            .all()
        )
        classification_dependencies = set(
            (
                await session.execute(
                    select(tables.stage_dependencies.c.dependency_key)
                    .select_from(
                        tables.stage_dependencies.join(tables.stage_artifacts).join(
                            tables.artifact_blobs
                        )
                    )
                    .where(tables.stage_artifacts.c.workflow_run_id == changed.run_id)
                    .where(
                        tables.artifact_blobs.c.content
                        == new["classification:site_report.txt"]["raw"]
                    )
                    .where(tables.stage_dependencies.c.kind == DependencyKind.SOURCE_VERSION.value)
                )
            ).scalars()
        )
    evidence = [json.loads(row) for row in evidence_rows]
    executed = {key for item in evidence for key in item["executed_block_keys"]}
    reused = {key for item in evidence for key in item["reused_block_keys"]}
    assert "classification:site_report.txt" in reused
    assert all(key not in executed for key in unaffected)
    assert unaffected <= reused
    assert any("installed_quantity" in key for key in executed)
    assert classification_dependencies
    assert classification_dependencies.isdisjoint(
        {f"source-chunk:{chunk_id}" for chunk_id in old_chunk_ids}
    )
    assert classification_dependencies <= {f"source-chunk:{chunk_id}" for chunk_id in new_chunk_ids}


async def test_rename_alias_then_edit_appends_one_document_lineage(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, _workflow, ingestion = review_context
    original = fixture_bytes()["site_report.txt"]
    first = await ingestion.ingest_pile(
        project_id=project_id, sources=(_source("site.txt", original),)
    )
    renamed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(
            IngestSource(
                name="renamed.txt",
                media_type="text/plain",
                content=original,
                previous_name="site.txt",
            ),
        ),
    )
    edited = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(_source("renamed.txt", original.replace(b"420 m", b"421 m")),),
    )

    assert renamed.files[0].status == "duplicate" and renamed.run_id is None
    assert edited.files[0].status == "parsed" and edited.files[0].version_number == 2
    assert first.files[0].document_id == renamed.files[0].document_id == edited.files[0].document_id
    async with sessions() as session:
        document_count = await session.scalar(
            select(func.count())
            .select_from(tables.documents)
            .where(tables.documents.c.project_id == project_id)
        )
        version_count = await session.scalar(
            select(func.count())
            .select_from(tables.document_versions)
            .join(tables.documents)
            .where(tables.documents.c.project_id == project_id)
        )
        aliases = set(
            (
                await session.execute(
                    select(tables.document_aliases.c.source_path).where(
                        tables.document_aliases.c.project_id == project_id
                    )
                )
            ).scalars()
        )
    assert document_count == 1
    assert version_count == 2
    assert aliases == {"site.txt", "renamed.txt"}


async def test_concurrent_same_content_names_create_distinct_lineages(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, _workflow, ingestion = review_context
    content = fixture_bytes()["site_report.txt"]

    first, second = await asyncio.gather(
        ingestion.ingest_pile(project_id=project_id, sources=(_source("first.txt", content),)),
        ingestion.ingest_pile(project_id=project_id, sources=(_source("second.txt", content),)),
    )

    assert sorted((first.files[0].status, second.files[0].status)) == ["parsed", "parsed"]
    assert first.files[0].document_id != second.files[0].document_id
    assert first.run_id is not None and second.run_id is not None
    assert first.run_id != second.run_id
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(tables.document_versions)
                .join(tables.documents)
                .where(tables.documents.c.project_id == project_id)
            )
            == 2
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(tables.workflow_runs)
                .where(tables.workflow_runs.c.project_id == project_id)
            )
            == 2
        )
        aliases = set(
            (
                await session.execute(
                    select(tables.document_aliases.c.source_path).where(
                        tables.document_aliases.c.project_id == project_id
                    )
                )
            ).scalars()
        )
    assert aliases == {"first.txt", "second.txt"}


async def test_path_ingestion_rejects_oversize_without_opening_and_defers_read_mutation(
    review_context, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, _workflow, _ingestion = review_context
    ingestion = IngestionService(sessions, workflow=_workflow, max_bytes=16)
    oversized = tmp_path / "oversized.txt"
    oversized.write_bytes(b"x" * 17)
    original_open = Path.open

    def guarded_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == oversized:
            raise AssertionError("oversized source must not be opened")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(IngestSource(name="oversized.txt", media_type="text/plain", path=oversized),),
    )
    assert result.files[0].status == "failed"
    assert result.files[0].problem is not None and result.files[0].problem.status == 413

    ingestion = IngestionService(sessions, workflow=_workflow, max_bytes=1_000)
    changing = tmp_path / "changing.txt"
    changing.write_bytes(b"Document type: Site Report\nInstalled quantity: 420 m\n")
    real_hash_stream = __import__(
        "fieldledger.services.ingestion_service", fromlist=["hash_stream"]
    ).hash_stream

    def mutating_hash_stream(stream, **kwargs):  # type: ignore[no-untyped-def]
        hashed = real_hash_stream(stream, **kwargs)
        changing.write_bytes(b"Document type: Site Report\nInstalled quantity: 421 m\n")
        return hashed

    monkeypatch.setattr("fieldledger.services.ingestion_service.hash_stream", mutating_hash_stream)
    deferred = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(IngestSource(name="changing.txt", media_type="text/plain", path=changing),),
    )
    assert deferred.files[0].status == "deferred"
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(tables.document_versions)
                .join(tables.documents)
                .where(tables.documents.c.project_id == project_id)
            )
            == 0
        )


async def test_watcher_hashes_same_size_preserved_mtime_changes(
    review_context, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, _workflow, ingestion = review_context
    source = tmp_path / "site.txt"
    source.write_bytes(b"Document type: Site Report\nInstalled quantity: 420 m\n")
    watcher = InboxWatcher(
        inbox=tmp_path,
        project_id=project_id,
        ingestion=ingestion,
        invalidation=InvalidationService(sessions),
        stability_window=datetime.resolution,
    )
    now = datetime(2026, 8, 23, tzinfo=UTC)
    assert (await watcher.scan(now=now)).files == ()
    first = await watcher.scan(now=now.replace(microsecond=1))
    original_stat = source.stat()
    source.write_bytes(b"Document type: Site Report\nInstalled quantity: 421 m\n")
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    second = await watcher.scan(now=now.replace(microsecond=2))

    assert first.files[0].status == "parsed"
    assert second.files[0].status == "parsed"
    assert second.files[0].version_number == 2


async def test_watcher_rename_and_change_never_reopens_historical_run(
    review_context, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    original = fixture_bytes()["site_report.txt"]
    source = tmp_path / "site.txt"
    source.write_bytes(original)
    invalidation = InvalidationService(sessions)
    watcher = InboxWatcher(
        inbox=tmp_path,
        project_id=project_id,
        ingestion=ingestion,
        invalidation=invalidation,
        stability_window=datetime.resolution * 0,
    )

    first = await watcher.scan(now=datetime(2026, 8, 23, tzinfo=UTC))
    assert first.run_id is not None
    executor = DeterministicStageExecutor(sessions)
    await _drain(workflow, executor)
    before = await _run_durable_state(sessions, first.run_id)
    assert before[0]["status"] == "review_ready"

    renamed_path = tmp_path / "renamed.txt"
    source.rename(renamed_path)
    renamed = await watcher.scan(now=datetime(2026, 8, 23, 0, 0, 1, tzinfo=UTC))
    assert renamed.run_id is None and renamed.files[0].status == "duplicate"
    renamed_path.write_bytes(original.replace(b"420 m", b"421 m"))
    changed = await watcher.scan(now=datetime(2026, 8, 23, 0, 0, 2, tzinfo=UTC))
    assert changed.run_id is not None
    await _drain(workflow, executor)

    after = await _run_durable_state(sessions, first.run_id)
    assert after == before
    async with sessions() as session:
        successor_attempt_runs = set(
            (
                    await session.execute(
                        select(tables.stage_attempts.c.workflow_run_id)
                        .join(tables.workflow_runs)
                        .where(
                            tables.workflow_runs.c.project_id == project_id,
                            tables.stage_attempts.c.workflow_run_id != first.run_id,
                        )
                    )
            ).scalars()
        )
    assert successor_attempt_runs == {changed.run_id}


async def test_concurrent_distinct_ingestions_create_unique_linear_successor_snapshots(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    initial = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(_source("site.txt", fixture_bytes()["site_report.txt"]),),
    )
    assert initial.run_id is not None
    await _drain(workflow, DeterministicStageExecutor(sessions))
    barrier_workflow = _BarrierWorkflow(sessions)
    ingestion.workflow = barrier_workflow
    fixtures = fixture_bytes()

    contract, invoice = await asyncio.gather(
        ingestion.ingest_pile(
            project_id=project_id,
            sources=(_source("contract.pdf", fixtures["contract.pdf"]),),
        ),
        ingestion.ingest_pile(
            project_id=project_id,
            sources=(_source("invoice.md", fixtures["invoice.md"]),),
        ),
    )

    assert contract.run_id is not None and invoice.run_id is not None
    async with sessions() as session:
        runs = (
            await session.execute(
                select(
                    tables.workflow_runs.c.id,
                    tables.workflow_runs.c.input_hash,
                    tables.workflow_runs.c.supersedes_run_id,
                    tables.workflow_runs.c.created_at,
                )
                .where(tables.workflow_runs.c.project_id == project_id)
                .order_by(
                    tables.workflow_runs.c.created_at,
                    tables.workflow_runs.c.id,
                )
            )
        ).all()
        manifest_rows = (
            await session.execute(
                select(
                    tables.run_input_manifest.c.workflow_run_id,
                    tables.run_input_manifest.c.kind,
                    tables.run_input_manifest.c.object_id,
                    tables.run_input_manifest.c.content_hash,
                ).where(
                    tables.run_input_manifest.c.workflow_run_id.in_(tuple(row.id for row in runs))
                )
            )
        ).all()
    manifest_by_run = {
        run.id: tuple(
            sorted(
                (
                    row.kind,
                    str(row.object_id),
                    row.content_hash,
                )
                for row in manifest_rows
                if row.workflow_run_id == run.id
            )
        )
        for run in runs
    }
    assert len(set(manifest_by_run.values())) == len(manifest_by_run)
    assert len({contract.run_id, invoice.run_id}) == 1
    assert len(runs) == 2
    assert runs[0].id == initial.run_id
    assert runs[1].id == contract.run_id
    assert runs[1].supersedes_run_id == runs[0].id
    assert {row.supersedes_run_id for row in runs if row.supersedes_run_id is not None} == {
        initial.run_id
    }


async def test_overlapped_distinct_manifests_follow_lineage_tip_not_created_time(
    review_context, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, workflow, ingestion = review_context
    initial = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(_source("site.txt", fixture_bytes()["site_report.txt"]),),
    )
    assert initial.run_id is not None
    await _drain(workflow, DeterministicStageExecutor(sessions))
    untracked = IngestionService(sessions, workflow=None, max_bytes=2_000_000)
    fixtures = fixture_bytes()
    assert (
        await untracked.ingest_file(
            project_id=project_id,
            source=_source("contract.pdf", fixtures["contract.pdf"]),
        )
    ).status == "parsed"

    original_manifest = WorkflowRepository.current_project_manifest
    first_manifest_loaded = asyncio.Event()
    release_first = asyncio.Event()
    manifest_reads = 0

    async def paused_manifest(
        repository: WorkflowRepository, target_project_id: UUID
    ):  # type: ignore[no-untyped-def]
        nonlocal manifest_reads
        manifest = await original_manifest(repository, target_project_id)
        manifest_reads += 1
        if manifest_reads == 1:
            first_manifest_loaded.set()
            await asyncio.wait_for(release_first.wait(), timeout=5)
        return manifest

    monkeypatch.setattr(
        WorkflowRepository, "current_project_manifest", paused_manifest
    )
    first_task = asyncio.create_task(
        workflow.create_incremental_run(
            project_id=project_id,
            now=datetime(2099, 1, 3, tzinfo=UTC),
        )
    )
    await asyncio.wait_for(first_manifest_loaded.wait(), timeout=5)
    assert (
        await untracked.ingest_file(
            project_id=project_id,
            source=_source("invoice.md", fixtures["invoice.md"]),
        )
    ).status == "parsed"
    second_task = asyncio.create_task(
        workflow.create_incremental_run(
            project_id=project_id,
            now=datetime(2099, 1, 1, tzinfo=UTC),
        )
    )
    await asyncio.sleep(0)
    release_first.set()
    first_run, second_run = await asyncio.wait_for(
        asyncio.gather(first_task, second_task), timeout=10
    )
    assert first_run is not None and second_run is not None

    retries = await asyncio.gather(
        workflow.create_incremental_run(project_id=project_id),
        workflow.create_incremental_run(project_id=project_id),
    )
    assert retries == [second_run, second_run]
    assert (
        await untracked.ingest_file(
            project_id=project_id,
            source=_source("change_order.docx", fixtures["change_order.docx"]),
        )
    ).status == "parsed"
    third_run = await workflow.create_incremental_run(
        project_id=project_id,
        now=datetime(2099, 1, 2, tzinfo=UTC),
    )
    assert third_run is not None
    assert await workflow.create_incremental_run(project_id=project_id) == third_run

    async with sessions() as session:
        rows = (
            await session.execute(
                select(
                    tables.workflow_runs.c.id,
                    tables.workflow_runs.c.supersedes_run_id,
                ).where(tables.workflow_runs.c.project_id == project_id)
            )
        ).all()
        manifests = (
            await session.execute(
                select(
                    tables.run_input_manifest.c.workflow_run_id,
                    tables.run_input_manifest.c.kind,
                    tables.run_input_manifest.c.object_id,
                    tables.run_input_manifest.c.content_hash,
                ).where(
                    tables.run_input_manifest.c.workflow_run_id.in_(
                        tuple(row.id for row in rows)
                    )
                )
            )
        ).all()
    predecessors = {row.id: row.supersedes_run_id for row in rows}
    assert predecessors == {
        initial.run_id: None,
        first_run: initial.run_id,
        second_run: first_run,
        third_run: second_run,
    }
    manifest_values = {
        run_id: tuple(
            sorted(
                (row.kind, str(row.object_id), row.content_hash)
                for row in manifests
                if row.workflow_run_id == run_id
            )
        )
        for run_id in predecessors
    }
    assert len(set(manifest_values.values())) == 4
    assert len(
        [parent for parent in predecessors.values() if parent is not None]
    ) == len(
        {parent for parent in predecessors.values() if parent is not None}
    )


async def test_invalidation_is_project_and_json_scoped_with_unrelated_binary_artifact(
    review_context,
) -> None:  # type: ignore[no-untyped-def]
    engine, sessions, project_id, workflow, ingestion = review_context
    original = fixture_bytes()["site_report.txt"]
    first = await ingestion.ingest_pile(
        project_id=project_id, sources=(_source("site.txt", original),)
    )
    assert first.run_id is not None
    executor = DeterministicStageExecutor(sessions)
    await _drain(workflow, executor)
    changed = await ingestion.ingest_pile(
        project_id=project_id,
        sources=(_source("site.txt", original.replace(b"420 m", b"421 m")),),
    )
    unrelated_project = uuid4()
    unrelated_run = uuid4()
    attempt_id = uuid4()
    blob_id = uuid4()
    link_id = uuid4()
    async with sessions() as session:
        old_chunk_id = next(
            iter(
                (
                    await session.execute(
                        select(tables.source_chunks.c.id).where(
                            tables.source_chunks.c.document_version_id
                            == first.files[0].document_version_id
                        )
                    )
                ).scalars()
            )
        )
    try:
        async with engine.begin() as connection:
            await connection.execute(
                insert(tables.projects).values(id=unrelated_project, name="Unrelated")
            )
            await connection.execute(
                insert(tables.workflow_runs).values(
                    id=unrelated_run,
                    project_id=unrelated_project,
                    status="review_ready",
                    input_hash="sha256:unrelated",
                )
            )
            await connection.execute(
                insert(tables.stage_attempts).values(
                    id=attempt_id,
                    workflow_run_id=unrelated_run,
                    stage="extract",
                    attempt_number=1,
                    status="completed",
                    input_hash="sha256:unrelated",
                )
            )
            binary = b"\xff\x00not-json"
            await connection.execute(
                insert(tables.artifact_blobs).values(
                    id=blob_id,
                    artifact_hash=f"sha256:{sha256(binary).hexdigest()}",
                    media_type="application/octet-stream",
                    storage_uri="postgresql://unrelated",
                    byte_size=len(binary),
                    content=binary,
                )
            )
            await connection.execute(
                insert(tables.stage_artifacts).values(
                    id=link_id,
                    workflow_run_id=unrelated_run,
                    stage_attempt_id=attempt_id,
                    artifact_blob_id=blob_id,
                    kind="fact_set",
                )
            )
            await connection.execute(
                insert(tables.stage_dependencies).values(
                    stage_artifact_id=link_id,
                    kind=DependencyKind.SOURCE_VERSION.value,
                    dependency_key=f"source-chunk:{old_chunk_id}",
                    content_hash="sha256:old",
                )
            )
        assert changed.run_id is not None
        plan = await InvalidationService(sessions).invalidate_ingestion(
            changed.files[0], successor_run_id=changed.run_id
        )
        assert plan is not None
        assert unrelated_run not in plan.run_ids
        assert plan.run_ids == (changed.run_id,)
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(tables.projects).where(tables.projects.c.id == unrelated_project)
            )
            await connection.execute(
                delete(tables.artifact_blobs).where(tables.artifact_blobs.c.id == blob_id)
            )


@pytest.mark.parametrize(
    "raw_rules",
    (
        b'{"version":"bad","rules":[{"id":"n","fact_key":"invoice_amount","operator":"gte","expected":"many"}]}',
        b'{"version":"bad","rules":[{"id":"d","fact_key":"revised_completion_date","operator":"date_on_or_before","expected":"soon"}]}',
    ),
)
async def test_invalid_operator_expected_type_is_visible_ingestion_failure(
    review_context, raw_rules: bytes
) -> None:  # type: ignore[no-untyped-def]
    _engine, sessions, project_id, _workflow, ingestion = review_context

    outcome = await ingestion.store_rule_set(project_id=project_id, raw_content=raw_rules)

    assert outcome.status == "failed"
    assert outcome.rule_set_id is None and outcome.run_id is None
    assert outcome.problem is not None and outcome.problem.status == 422
    async with sessions() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(tables.rule_sets)
                .where(tables.rule_sets.c.project_id == project_id)
            )
            == 0
        )
        recorded = (
            await session.execute(
                select(tables.ingestion_outcomes.c.status, tables.ingestion_outcomes.c.problem)
                .where(tables.ingestion_outcomes.c.project_id == project_id)
                .where(tables.ingestion_outcomes.c.source_name == "rules.json")
            )
        ).one()
    assert recorded.status == "failed"
    assert recorded.problem["status"] == 422
