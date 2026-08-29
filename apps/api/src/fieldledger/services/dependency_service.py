from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.domain.models import DependencyKind
from fieldledger.domain.review import DependencyEntry, DependencyVector
from fieldledger.persistence.tables import (
    artifact_blobs,
    document_versions,
    documents,
    review_batches,
    review_dependencies,
    rule_sets,
    run_input_manifest,
    stage_artifacts,
)


class DependencyService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        policy_content_hash: str,
    ) -> None:
        self.sessions = sessions
        self.policy_content_hash = policy_content_hash

    async def reviewed_vector(
        self, batch_id: UUID, *, session: AsyncSession | None = None
    ) -> DependencyVector:
        if session is not None:
            return await self._reviewed_vector(session, batch_id)
        async with self.sessions() as owned:
            return await self._reviewed_vector(owned, batch_id)

    async def current_vector(
        self, batch_id: UUID, *, session: AsyncSession | None = None
    ) -> DependencyVector:
        if session is not None:
            return await self._current_vector(session, batch_id)
        async with self.sessions() as owned:
            return await self._current_vector(owned, batch_id)

    @staticmethod
    async def _reviewed_vector(session: AsyncSession, batch_id: UUID) -> DependencyVector:
        rows = (
            await session.execute(
                select(
                    review_dependencies.c.kind,
                    review_dependencies.c.dependency_key,
                    review_dependencies.c.content_hash,
                ).where(review_dependencies.c.review_batch_id == batch_id)
            )
        ).all()
        return DependencyVector(
            entries=tuple(
                DependencyEntry(
                    kind=DependencyKind(row.kind),
                    key=row.dependency_key,
                    content_hash=row.content_hash,
                )
                for row in rows
            )
        )

    async def _current_vector(
        self, session: AsyncSession, batch_id: UUID
    ) -> DependencyVector:
        batch = (
            await session.execute(
                select(review_batches).where(review_batches.c.id == batch_id)
            )
        ).mappings().one_or_none()
        if batch is None:
            return DependencyVector(entries=())
        reviewed = await self._reviewed_vector(session, batch_id)
        current: list[DependencyEntry] = []
        for entry in reviewed.entries:
            content_hash = await self._resolve_hash(session, batch, entry)
            if content_hash is not None:
                current.append(entry.model_copy(update={"content_hash": content_hash}))
        return DependencyVector(entries=tuple(current))

    async def _resolve_hash(
        self,
        session: AsyncSession,
        batch: Any,
        entry: DependencyEntry,
    ) -> str | None:
        if entry.kind in {
            DependencyKind.TARGET_DOCUMENT_VERSION,
            DependencyKind.SOURCE_VERSION,
        }:
            try:
                document_id = UUID(entry.key)
            except ValueError:
                return None
            value = await session.scalar(
                select(document_versions.c.content_hash)
                .join(documents, documents.c.id == document_versions.c.document_id)
                .where(
                    documents.c.id == document_id,
                    documents.c.project_id == batch["project_id"],
                )
                .order_by(document_versions.c.version_number.desc())
                .limit(1)
            )
            return str(value) if value is not None else None
        if entry.kind is DependencyKind.RULE_SET_VERSION:
            value = await session.scalar(
                select(rule_sets.c.content_hash)
                .where(rule_sets.c.project_id == batch["project_id"])
                .order_by(rule_sets.c.version_number.desc())
                .limit(1)
            )
            return str(value) if value is not None else None
        if entry.kind is DependencyKind.POLICY_VERSION:
            return self.policy_content_hash
        if entry.kind is DependencyKind.PROPOSAL:
            return str(batch["proposal_hash"]) if batch["proposal_hash"] is not None else None
        if entry.kind is DependencyKind.WORKFLOW_ARTIFACT:
            try:
                artifact_id = UUID(entry.key)
            except ValueError:
                return None
            value = await session.scalar(
                select(artifact_blobs.c.artifact_hash)
                .select_from(stage_artifacts.join(artifact_blobs))
                .where(
                    stage_artifacts.c.id == artifact_id,
                    stage_artifacts.c.workflow_run_id == batch["workflow_run_id"],
                )
            )
            return str(value) if value is not None else None
        return None

    async def snapshot_for_proposal(
        self,
        *,
        project_id: UUID,
        target_document_id: UUID,
        target_document_version_id: UUID | None,
        workflow_run_id: UUID,
        batch_id: UUID,
        proposal_hash: str,
        session: AsyncSession,
    ) -> DependencyVector:
        version_rows = (
            await session.execute(
                select(
                    documents.c.id,
                    document_versions.c.id.label("version_id"),
                    document_versions.c.content_hash,
                )
                .join(document_versions)
                .join(
                    run_input_manifest,
                    run_input_manifest.c.object_id == document_versions.c.id,
                )
                .where(
                    documents.c.project_id == project_id,
                    run_input_manifest.c.workflow_run_id == workflow_run_id,
                    run_input_manifest.c.kind == DependencyKind.SOURCE_VERSION.value,
                )
            )
        ).all()
        if not version_rows:
            fallback = (
                await session.execute(
                    select(
                        documents.c.id,
                        document_versions.c.id.label("version_id"),
                        document_versions.c.content_hash,
                        document_versions.c.version_number,
                    )
                    .join(document_versions)
                    .where(documents.c.project_id == project_id)
                    .order_by(documents.c.id, document_versions.c.version_number.desc())
                )
            ).all()
            latest: dict[UUID, Any] = {}
            for row in fallback:
                latest.setdefault(row.id, row)
            version_rows = list(latest.values())
        entries = [
            DependencyEntry(
                kind=(
                    DependencyKind.TARGET_DOCUMENT_VERSION
                    if row.id == target_document_id
                    else DependencyKind.SOURCE_VERSION
                ),
                key=str(row.id),
                content_hash=row.content_hash,
            )
            for row in version_rows
            if target_document_version_id is None
            or row.id != target_document_id
            or row.version_id == target_document_version_id
        ]
        rule_hash = await session.scalar(
            select(rule_sets.c.content_hash)
            .where(rule_sets.c.project_id == project_id)
            .order_by(rule_sets.c.version_number.desc())
            .limit(1)
        )
        if rule_hash is not None:
            entries.append(
                DependencyEntry(
                    kind=DependencyKind.RULE_SET_VERSION,
                    key=str(project_id),
                    content_hash=str(rule_hash),
                )
            )
        artifact_rows = (
            await session.execute(
                select(stage_artifacts.c.id, artifact_blobs.c.artifact_hash)
                .select_from(stage_artifacts.join(artifact_blobs))
                .where(stage_artifacts.c.workflow_run_id == workflow_run_id)
            )
        ).all()
        entries.extend(
            DependencyEntry(
                kind=DependencyKind.WORKFLOW_ARTIFACT,
                key=str(row.id),
                content_hash=row.artifact_hash,
            )
            for row in artifact_rows
        )
        entries.extend(
            [
                DependencyEntry(
                    kind=DependencyKind.POLICY_VERSION,
                    key="fieldledger-policy",
                    content_hash=self.policy_content_hash,
                ),
                DependencyEntry(
                    kind=DependencyKind.PROPOSAL,
                    key=str(batch_id),
                    content_hash=proposal_hash,
                ),
            ]
        )
        return DependencyVector(entries=tuple(entries))
