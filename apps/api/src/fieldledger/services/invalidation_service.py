from __future__ import annotations

import json
from collections.abc import Iterator
from uuid import UUID

from sqlalchemy import RowMapping, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.domain.models import ArtifactKind, ContractModel, DependencyKind, StageName
from fieldledger.domain.workflow import STAGE_ORDER
from fieldledger.persistence.tables import (
    artifact_blobs,
    run_input_manifest,
    run_invalidation_entries,
    source_chunks,
    stage_artifacts,
    stage_attempts,
    stage_dependencies,
    workflow_runs,
)
from fieldledger.services.ingestion_service import FileIngestionOutcome


class InvalidationPlan(ContractModel):
    run_ids: tuple[UUID, ...]
    earliest_stage: StageName
    affected_block_keys: tuple[str, ...]


class InvalidationService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
    ) -> None:
        self.sessions = sessions

    async def invalidate_ingestion(
        self, outcome: FileIngestionOutcome, *, successor_run_id: UUID
    ) -> InvalidationPlan | None:
        if (
            outcome.status != "parsed"
            or outcome.previous_version_id is None
            or outcome.document_version_id is None
            or outcome.content_hash is None
        ):
            return None
        changed_dependency_keys = await self._changed_chunk_keys(
            outcome.previous_version_id, outcome.document_version_id
        )
        if not changed_dependency_keys:
            return None
        if outcome.project_id is None:
            return None
        async with self.sessions() as session:
            successor = (
                await session.execute(
                    select(
                        workflow_runs.c.project_id,
                        workflow_runs.c.supersedes_run_id,
                    ).where(workflow_runs.c.id == successor_run_id)
                )
            ).one_or_none()
            pins_version = await session.scalar(
                select(run_input_manifest.c.id)
                .where(run_input_manifest.c.workflow_run_id == successor_run_id)
                .where(
                    run_input_manifest.c.kind
                    == DependencyKind.SOURCE_VERSION.value
                )
                .where(
                    run_input_manifest.c.object_id == outcome.document_version_id
                )
                .where(run_input_manifest.c.content_hash == outcome.content_hash)
                .limit(1)
            )
        if successor is None or successor.project_id != outcome.project_id:
            raise ValueError("successor run does not belong to the ingestion project")
        if pins_version is None:
            raise ValueError("successor run does not pin the ingested document version")
        predecessor_run_id = successor.supersedes_run_id
        if predecessor_run_id is None:
            return None
        affected_by_run = await self._dependency_closure(
            outcome.project_id,
            changed_dependency_keys,
            source_run_id=predecessor_run_id,
        )
        if not affected_by_run:
            return None
        earliest: StageName | None = None
        all_blocks: set[str] = set()
        for stage, blocks in affected_by_run.values():
            all_blocks.update(blocks)
            if earliest is None or STAGE_ORDER.index(stage) < STAGE_ORDER.index(earliest):
                earliest = stage
        if earliest is None:
            return None
        async with self.sessions.begin() as session:
            await session.execute(
                postgres_insert(run_invalidation_entries)
                .values(
                    [
                        {
                            "workflow_run_id": successor_run_id,
                            "document_version_id": outcome.document_version_id,
                            "previous_document_version_id": outcome.previous_version_id,
                            "earliest_stage": earliest.value,
                            "affected_block_key": block_key,
                        }
                        for block_key in sorted(all_blocks)
                    ]
                )
                .on_conflict_do_nothing(
                    constraint="uq_run_invalidation_entry"
                )
            )
        return InvalidationPlan(
            run_ids=(successor_run_id,),
            earliest_stage=earliest,
            affected_block_keys=tuple(sorted(all_blocks)),
        )

    async def _changed_chunk_keys(
        self, previous_version_id: UUID, current_version_id: UUID
    ) -> tuple[str, ...]:
        async with self.sessions() as session:
            previous = (
                await session.execute(
                    select(
                        source_chunks.c.id,
                        source_chunks.c.coordinate,
                        source_chunks.c.content_hash,
                    ).where(source_chunks.c.document_version_id == previous_version_id)
                )
            ).mappings().all()
            current = (
                await session.execute(
                    select(source_chunks.c.coordinate, source_chunks.c.content_hash).where(
                        source_chunks.c.document_version_id == current_version_id
                    )
                )
            ).mappings().all()
        current_signatures = {
            (
                json.dumps(row["coordinate"], sort_keys=True, separators=(",", ":")),
                row["content_hash"],
            )
            for row in current
        }
        return tuple(
            f"source-chunk:{row['id']}"
            for row in previous
            if (
                json.dumps(row["coordinate"], sort_keys=True, separators=(",", ":")),
                row["content_hash"],
            )
            not in current_signatures
        )

    async def _dependency_closure(
        self,
        project_id: UUID,
        dependency_keys: tuple[str, ...],
        *,
        source_run_id: UUID,
    ) -> dict[UUID, tuple[StageName, set[str]]]:
        block_kinds = tuple(
            item.value
            for item in ArtifactKind
            if item
            not in {ArtifactKind.ANALYSIS_SNAPSHOT, ArtifactKind.EXECUTION_EVIDENCE}
        )

        async def query_nodes(
            *,
            kind: DependencyKind,
            keys: set[str],
            run_id: UUID | None = None,
        ) -> tuple[RowMapping, ...]:
            if not keys:
                return ()
            statement = (
                select(
                    stage_artifacts.c.workflow_run_id,
                    stage_attempts.c.stage,
                    artifact_blobs.c.content,
                )
                .select_from(
                    stage_dependencies.join(stage_artifacts)
                    .join(
                        stage_attempts,
                        stage_attempts.c.id == stage_artifacts.c.stage_attempt_id,
                    )
                    .join(
                        artifact_blobs,
                        artifact_blobs.c.id == stage_artifacts.c.artifact_blob_id,
                    )
                    .join(
                        workflow_runs,
                        workflow_runs.c.id == stage_artifacts.c.workflow_run_id,
                    )
                )
                .where(workflow_runs.c.project_id == project_id)
                .where(stage_dependencies.c.kind == kind.value)
                .where(stage_dependencies.c.dependency_key.in_(keys))
                .where(stage_artifacts.c.kind.in_(block_kinds))
                .where(artifact_blobs.c.media_type == "application/json")
            )
            if run_id is not None:
                statement = statement.where(stage_artifacts.c.workflow_run_id == run_id)
            async with self.sessions() as session:
                return tuple((await session.execute(statement)).mappings())

        def parsed_nodes(
            rows: tuple[RowMapping, ...],
        ) -> Iterator[tuple[UUID, StageName, str]]:
            for row in rows:
                try:
                    payload = json.loads(row["content"])
                except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                block_key = payload.get("block_key")
                if not isinstance(block_key, str):
                    continue
                yield row["workflow_run_id"], StageName(row["stage"]), block_key

        affected: dict[UUID, set[str]] = {}
        stage_by_block: dict[tuple[UUID, str], StageName] = {}
        frontier_by_run: dict[UUID, set[str]] = {}
        initial_rows = await query_nodes(
            kind=DependencyKind.SOURCE_VERSION,
            keys=set(dependency_keys),
            run_id=source_run_id,
        )
        for run_id, stage, block_key in parsed_nodes(initial_rows):
            affected.setdefault(run_id, set()).add(block_key)
            stage_by_block[(run_id, block_key)] = stage
            frontier_by_run.setdefault(run_id, set()).add(f"artifact:{block_key}")

        while frontier_by_run:
            next_by_run: dict[UUID, set[str]] = {}
            for run_id, frontier in frontier_by_run.items():
                rows = await query_nodes(
                    kind=DependencyKind.PROPOSAL,
                    keys=frontier,
                    run_id=run_id,
                )
                for _, stage, block_key in parsed_nodes(rows):
                    run_affected = affected.setdefault(run_id, set())
                    if block_key in run_affected:
                        continue
                    run_affected.add(block_key)
                    stage_by_block[(run_id, block_key)] = stage
                    next_by_run.setdefault(run_id, set()).add(f"artifact:{block_key}")
            frontier_by_run = next_by_run

        result: dict[UUID, tuple[StageName, set[str]]] = {}
        for run_id, blocks in affected.items():
            stages = [stage_by_block[(run_id, block)] for block in blocks]
            result[run_id] = (min(stages, key=STAGE_ORDER.index), blocks)
        return result
