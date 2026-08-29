from __future__ import annotations

import json
import os
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal, cast
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import func, insert, select, text
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.agent.rules import RuleSet
from fieldledger.domain.models import (
    ContractModel,
    ProblemDetails,
)
from fieldledger.parsers import default_registry
from fieldledger.parsers.base import HashedContent, SourceTooLargeError, hash_bytes, hash_stream
from fieldledger.persistence.tables import (
    document_aliases,
    document_versions,
    documents,
    ingestion_outcomes,
    rule_sets,
    source_chunks,
)
from fieldledger.services.mutation_fence import acquire_project_mutation_fence
from fieldledger.services.workflow_service import WorkflowService


@dataclass(frozen=True)
class IngestSource:
    name: str
    media_type: str
    content: bytes | None = None
    path: Path | None = None
    previous_name: str | None = None

    def __post_init__(self) -> None:
        if (self.content is None) == (self.path is None):
            raise ValueError("exactly one of content or path is required")


class FileIngestionOutcome(ContractModel):
    project_id: UUID | None = None
    source_name: str
    status: Literal["parsed", "unsupported", "failed", "duplicate", "deferred"]
    content_hash: str | None = None
    document_id: UUID | None = None
    document_version_id: UUID | None = None
    version_number: int | None = None
    previous_version_id: UUID | None = None
    previous_content_hash: str | None = None
    problem: ProblemDetails | None = None


class RuleSetIngestionOutcome(ContractModel):
    status: Literal["stored", "duplicate", "failed"]
    content_hash: str
    rule_set_id: UUID | None = None
    version_number: int | None = None
    run_id: UUID | None = None
    problem: ProblemDetails | None = None


class PileIngestionResult(ContractModel):
    files: tuple[FileIngestionOutcome, ...]
    run_id: UUID | None


class _SourceChangedDuringRead(RuntimeError):
    def __init__(self, hashed: HashedContent) -> None:
        super().__init__("source identity, size, or modification time changed during read")
        self.hashed = hashed


def _problem(status: int, title: str, detail: str) -> ProblemDetails:
    return ProblemDetails(
        type=f"https://fieldledger.local/problems/{title.lower().replace(' ', '-')}",
        title=title,
        status=status,
        detail=detail,
    )


def _safe_name(name: str) -> bool:
    return bool(name.strip()) and Path(name).name == name and name not in {".", ".."}


def _path_signature(stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        stat.st_dev,
        stat.st_ino,
        stat.st_size,
        stat.st_mtime_ns,
    )


class IngestionService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        workflow: WorkflowService | None = None,
        max_bytes: int = 25 * 1024 * 1024,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self.sessions = sessions
        self.workflow = workflow
        self.max_bytes = max_bytes
        self.registry = default_registry()

    async def ingest_pile(
        self, *, project_id: UUID, sources: tuple[IngestSource, ...]
    ) -> PileIngestionResult:
        outcomes: list[FileIngestionOutcome] = []
        for source in sources:
            try:
                outcome = await self.ingest_file(project_id=project_id, source=source)
            except Exception as error:
                outcome = FileIngestionOutcome(
                    project_id=project_id,
                    source_name=source.name,
                    status="failed",
                    content_hash=(
                        hash_bytes(source.content) if source.content is not None else None
                    ),
                    problem=_problem(
                        500,
                        "Ingestion failed",
                        f"{type(error).__name__}: {error}",
                    ),
                )
                await self._record(project_id, outcome)
            outcomes.append(outcome)

        run_id: UUID | None = None
        if self.workflow is not None and any(item.status == "parsed" for item in outcomes):
            run_id = await self._create_successor_run(project_id)
        return PileIngestionResult(files=tuple(outcomes), run_id=run_id)

    async def ingest_file(
        self, *, project_id: UUID, source: IngestSource
    ) -> FileIngestionOutcome:
        if not _safe_name(source.name):
            return await self._reject(
                project_id,
                source,
                "failed",
                _problem(400, "Invalid source path", "source name must be a basename"),
            )
        type_problem = self.registry.validate_type(source.name, source.media_type)
        if type_problem is not None:
            return await self._reject(project_id, source, "unsupported", type_problem)
        try:
            hashed = self._read_source(source)
        except SourceTooLargeError as error:
            return await self._reject(
                project_id,
                source,
                "failed",
                _problem(413, "Source too large", str(error)),
            )
        except _SourceChangedDuringRead as error:
            return await self._reject(
                project_id,
                source,
                "deferred",
                _problem(409, "Source changed during read", str(error)),
                content_hash=error.hashed.content_hash,
            )
        if hashed.size_bytes == 0:
            return await self._reject(
                project_id,
                source,
                "failed",
                _problem(422, "Empty source", "source contains zero bytes"),
                content_hash=hashed.content_hash,
            )

        parsed = self.registry.parse(source.name, source.media_type, hashed.content)
        if parsed.status != "parsed":
            return await self._reject(
                project_id,
                source,
                parsed.status,
                parsed.problem
                or _problem(422, "Source parse failed", "parser returned no problem"),
                content_hash=hashed.content_hash,
            )

        normalized_path = source.name.casefold()
        async with self.sessions.begin() as session:
            await acquire_project_mutation_fence(session, project_id)
            await self._lock_ingestion_keys(
                session, project_id, normalized_path, hashed.content_hash
            )
            alias = (
                await session.execute(
                    select(document_aliases)
                    .where(document_aliases.c.project_id == project_id)
                    .where(document_aliases.c.normalized_path == normalized_path)
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if alias is None and source.previous_name is not None:
                alias = (
                    await session.execute(
                        select(document_aliases)
                        .where(document_aliases.c.project_id == project_id)
                        .where(
                            document_aliases.c.normalized_path
                            == source.previous_name.casefold()
                        )
                        .with_for_update()
                    )
                ).mappings().one_or_none()
            duplicate = None
            if alias is not None:
                duplicate = (
                    await session.execute(
                        select(document_versions)
                        .where(document_versions.c.document_id == alias["document_id"])
                        .where(document_versions.c.content_hash == hashed.content_hash)
                        .limit(1)
                    )
                ).mappings().one_or_none()
            if duplicate is not None:
                await self._upsert_alias(
                    session,
                    project_id=project_id,
                    document_id=duplicate["document_id"],
                    source_name=source.name,
                    normalized_path=normalized_path,
                )
                outcome = FileIngestionOutcome(
                    project_id=project_id,
                    source_name=source.name,
                    status="duplicate",
                    content_hash=hashed.content_hash,
                    document_id=duplicate["document_id"],
                    document_version_id=duplicate["id"],
                    version_number=duplicate["version_number"],
                )
                await self._insert_outcome(session, project_id, outcome)
                return outcome

            if alias is None:
                fingerprint = hash_bytes(normalized_path.encode())
                document_id = cast(
                    UUID,
                    (
                        await session.execute(
                            insert(documents)
                            .values(
                                project_id=project_id,
                                name=source.name,
                                media_type=source.media_type,
                                source_path=source.name,
                                fingerprint=fingerprint,
                            )
                            .returning(documents.c.id)
                        )
                    ).scalar_one(),
                )
                previous = None
                await self._upsert_alias(
                    session,
                    project_id=project_id,
                    document_id=document_id,
                    source_name=source.name,
                    normalized_path=normalized_path,
                )
            else:
                document_id = cast(UUID, alias["document_id"])
                previous = (
                    await session.execute(
                        select(document_versions)
                        .where(document_versions.c.document_id == document_id)
                        .order_by(document_versions.c.version_number.desc())
                        .limit(1)
                        .with_for_update()
                    )
                ).mappings().one_or_none()
                await self._upsert_alias(
                    session,
                    project_id=project_id,
                    document_id=document_id,
                    source_name=source.name,
                    normalized_path=normalized_path,
                )

            version_number = 1 if previous is None else previous["version_number"] + 1
            version_id = cast(
                UUID,
                (
                    await session.execute(
                        insert(document_versions)
                        .values(
                            document_id=document_id,
                            version_number=version_number,
                            content_hash=hashed.content_hash,
                            storage_uri=f"postgresql://document-versions/{hashed.content_hash}",
                            byte_size=hashed.size_bytes,
                            raw_content=hashed.content,
                        )
                        .returning(document_versions.c.id)
                    )
                ).scalar_one(),
            )
            await session.execute(
                insert(source_chunks),
                [
                    {
                        "document_version_id": version_id,
                        "ordinal": chunk.ordinal,
                        "content_hash": chunk.content_hash,
                        "content": chunk.content,
                        "coordinate": chunk.coordinate.model_dump(mode="json", exclude_none=True),
                    }
                    for chunk in parsed.chunks
                ],
            )
            outcome = FileIngestionOutcome(
                project_id=project_id,
                source_name=source.name,
                status="parsed",
                content_hash=hashed.content_hash,
                document_id=document_id,
                document_version_id=version_id,
                version_number=version_number,
                previous_version_id=previous["id"] if previous is not None else None,
                previous_content_hash=previous["content_hash"] if previous is not None else None,
            )
            await self._insert_outcome(session, project_id, outcome)
        return outcome

    async def store_rule_set(
        self, *, project_id: UUID, raw_content: bytes
    ) -> RuleSetIngestionOutcome:
        content_hash = hash_bytes(raw_content)
        try:
            raw = json.loads(raw_content.decode("utf-8"))
            parsed = RuleSet.model_validate(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
            problem = _problem(422, "Invalid rule set", f"{type(error).__name__}: {error}")
            outcome = RuleSetIngestionOutcome(
                status="failed", content_hash=content_hash, problem=problem
            )
            await self._record_rule_outcome(project_id, outcome)
            return outcome

        async with self.sessions.begin() as session:
            await acquire_project_mutation_fence(session, project_id)
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"rules:{project_id}"},
            )
            duplicate = (
                await session.execute(
                    select(rule_sets.c.id, rule_sets.c.version_number)
                    .where(rule_sets.c.project_id == project_id)
                    .where(rule_sets.c.content_hash == content_hash)
                )
            ).one_or_none()
            if duplicate is not None:
                return RuleSetIngestionOutcome(
                    status="duplicate",
                    content_hash=content_hash,
                    rule_set_id=duplicate.id,
                    version_number=duplicate.version_number,
                )
            version_number = (
                await session.scalar(
                    select(func.max(rule_sets.c.version_number)).where(
                        rule_sets.c.project_id == project_id
                    )
                )
                or 0
            ) + 1
            rule_set_id = cast(
                UUID,
                (
                    await session.execute(
                        insert(rule_sets)
                        .values(
                            project_id=project_id,
                            version_number=version_number,
                            version=parsed.version,
                            content_hash=content_hash,
                            raw_content=raw_content,
                            rules=parsed.model_dump(mode="json"),
                        )
                        .returning(rule_sets.c.id)
                    )
                ).scalar_one(),
            )
        run_id = (
            await self._create_successor_run(project_id)
            if self.workflow is not None and await self._project_has_documents(project_id)
            else None
        )
        return RuleSetIngestionOutcome(
            status="stored",
            content_hash=content_hash,
            rule_set_id=rule_set_id,
            version_number=version_number,
            run_id=run_id,
        )

    def _read_source(self, source: IngestSource) -> HashedContent:
        if source.content is not None:
            return hash_stream(BytesIO(source.content), max_bytes=self.max_bytes)
        if source.path is None:
            raise RuntimeError("source has neither bytes nor a path")
        before = source.path.stat()
        if before.st_size > self.max_bytes:
            raise SourceTooLargeError(f"source exceeds {self.max_bytes} bytes")
        with source.path.open("rb") as stream:
            hashed = hash_stream(stream, max_bytes=self.max_bytes)
        after = source.path.stat()
        if (
            _path_signature(before) != _path_signature(after)
            or hashed.size_bytes != before.st_size
        ):
            raise _SourceChangedDuringRead(hashed)
        return hashed

    @staticmethod
    async def _lock_ingestion_keys(
        session: AsyncSession,
        project_id: UUID,
        normalized_path: str,
        content_hash: str,
    ) -> None:
        keys = sorted(
            {
                f"content:{project_id}:{content_hash}",
                f"path:{project_id}:{normalized_path}",
            }
        )
        for key in keys:
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": key},
            )

    @staticmethod
    async def _upsert_alias(
        session: AsyncSession,
        *,
        project_id: UUID,
        document_id: UUID,
        source_name: str,
        normalized_path: str,
    ) -> None:
        await session.execute(
            postgres_insert(document_aliases)
            .values(
                project_id=project_id,
                document_id=document_id,
                source_path=source_name,
                normalized_path=normalized_path,
            )
            .on_conflict_do_update(
                index_elements=[
                    document_aliases.c.project_id,
                    document_aliases.c.normalized_path,
                ],
                set_={
                    "document_id": document_id,
                    "source_path": source_name,
                    "last_seen_at": func.now(),
                },
            )
        )

    async def _project_has_documents(self, project_id: UUID) -> bool:
        async with self.sessions() as session:
            return bool(
                await session.scalar(
                    select(func.count()).select_from(documents).where(
                        documents.c.project_id == project_id
                    )
                )
            )

    async def _create_successor_run(self, project_id: UUID) -> UUID | None:
        if self.workflow is None:
            return None
        return await self.workflow.create_incremental_run(project_id=project_id)

    async def _reject(
        self,
        project_id: UUID,
        source: IngestSource,
        status: Literal["unsupported", "failed", "deferred"],
        problem: ProblemDetails,
        *,
        content_hash: str | None = None,
    ) -> FileIngestionOutcome:
        outcome = FileIngestionOutcome(
            project_id=project_id,
            source_name=source.name,
            status=status,
            content_hash=(
                content_hash
                if content_hash is not None
                else hash_bytes(source.content)
                if source.content is not None
                else None
            ),
            problem=problem,
        )
        await self._record(project_id, outcome)
        return outcome

    async def _record_rule_outcome(
        self, project_id: UUID, outcome: RuleSetIngestionOutcome
    ) -> None:
        async with self.sessions.begin() as session:
            await session.execute(
                insert(ingestion_outcomes).values(
                    project_id=project_id,
                    source_name="rules.json",
                    status=outcome.status,
                    content_hash=outcome.content_hash,
                    problem=(
                        outcome.problem.model_dump(mode="json") if outcome.problem else None
                    ),
                )
            )

    async def _record(self, project_id: UUID, outcome: FileIngestionOutcome) -> None:
        async with self.sessions.begin() as session:
            await self._insert_outcome(session, project_id, outcome)

    @staticmethod
    async def _insert_outcome(
        session: AsyncSession, project_id: UUID, outcome: FileIngestionOutcome
    ) -> None:
        await session.execute(
            insert(ingestion_outcomes).values(
                project_id=project_id,
                source_name=outcome.source_name,
                status=outcome.status,
                content_hash=outcome.content_hash,
                document_version_id=outcome.document_version_id,
                problem=(
                    outcome.problem.model_dump(mode="json") if outcome.problem else None
                ),
            )
        )
