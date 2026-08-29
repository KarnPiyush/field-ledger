from __future__ import annotations

from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.persistence.tables import (
    audit_events,
    idempotency_keys,
    project_memberships,
    projects,
    workflow_runs,
)
from fieldledger.security import Actor, ProjectAuthorizer, ProjectRole
from fieldledger.services.ingestion_service import IngestionService, IngestSource
from fieldledger.services.workflow_service import WorkflowService


class ProjectService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authorization: ProjectAuthorizer,
    ) -> None:
        self.sessions = sessions
        self.authorization = authorization

    async def create_project(
        self, *, name: str, description: str | None, actor: Actor
    ) -> dict[str, Any]:
        project_id = uuid4()
        async with self.sessions.begin() as session:
            await session.execute(
                insert(projects).values(id=project_id, name=name, description=description)
            )
            await session.execute(
                insert(project_memberships).values(
                    project_id=project_id,
                    actor_id=actor.actor_id,
                    role=ProjectRole.REVIEWER.value,
                    granted_by=actor.actor_id,
                )
            )
            row = (
                await session.execute(select(projects).where(projects.c.id == project_id))
            ).mappings().one()
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "status": row["status"],
        }

    async def ensure_project(
        self,
        *,
        project_id: UUID,
        name: str,
        description: str | None,
        actor: Actor,
        role: ProjectRole = ProjectRole.AGENT,
    ) -> dict[str, Any]:
        async with self.sessions.begin() as session:
            await session.execute(
                postgres_insert(projects)
                .values(id=project_id, name=name, description=description)
                .on_conflict_do_nothing(index_elements=[projects.c.id])
            )
            await session.execute(
                postgres_insert(project_memberships)
                .values(
                    project_id=project_id,
                    actor_id=actor.actor_id,
                    role=role.value,
                    granted_by=actor.actor_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        project_memberships.c.project_id,
                        project_memberships.c.actor_id,
                    ]
                )
            )
            row = (
                await session.execute(select(projects).where(projects.c.id == project_id))
            ).mappings().one()
        return {
            "id": row["id"],
            "name": row["name"],
            "description": row["description"],
            "status": row["status"],
        }

    async def list_projects(
        self, actor: Actor, *, limit: int, offset: int
    ) -> tuple[list[dict[str, Any]], int]:
        async with self.sessions() as session:
            statement = (
                select(projects)
                .join(
                    project_memberships,
                    project_memberships.c.project_id == projects.c.id,
                )
                .where(project_memberships.c.actor_id == actor.actor_id)
            )
            total = int(
                await session.scalar(
                    select(func.count())
                    .select_from(project_memberships)
                    .where(project_memberships.c.actor_id == actor.actor_id)
                )
                or 0
            )
            rows = (
                await session.execute(statement.limit(limit).offset(offset))
            ).mappings().all()
        return (
            [
                {
                    "id": row["id"],
                    "name": row["name"],
                    "description": row["description"],
                    "status": row["status"],
                }
                for row in rows
            ],
            total,
        )


class SourceService:
    def __init__(self, ingestion: IngestionService, authorization: ProjectAuthorizer) -> None:
        self.ingestion = ingestion
        self.authorization = authorization

    async def add_source(
        self,
        project_id: UUID,
        *,
        name: str,
        media_type: str,
        content: bytes,
        actor: Actor,
    ) -> dict[str, Any]:
        await self.authorization.require(
            actor,
            project_id,
            roles={ProjectRole.REVIEWER, ProjectRole.OPERATOR, ProjectRole.AGENT},
        )
        result = await self.ingestion.ingest_pile(
            project_id=project_id,
            sources=(IngestSource(name=name, media_type=media_type, content=content),),
        )
        outcome = result.files[0].model_dump(mode="json")
        outcome["run_id"] = str(result.run_id) if result.run_id is not None else None
        return outcome

    async def add_fixture_source(
        self, project_id: UUID, *, fixture_name: str, actor: Actor
    ) -> dict[str, Any]:
        fixtures = {
            "site_report.txt": (
                b"Document type: Site Report\nInstalled quantity: 420 m\n"
            ),
            "invoice.md": (
                b"# Invoice\nDocument type: Invoice\nInvoice amount: USD 122500\n"
            ),
        }
        try:
            content = fixtures[fixture_name]
        except KeyError as error:
            raise ValueError("unknown deterministic fixture") from error
        media_type = "text/markdown" if fixture_name.endswith(".md") else "text/plain"
        return await self.add_source(
            project_id,
            name=fixture_name,
            media_type=media_type,
            content=content,
            actor=actor,
        )


class RunService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        workflow: WorkflowService,
        authorization: ProjectAuthorizer,
    ) -> None:
        self.sessions = sessions
        self.workflow = workflow
        self.authorization = authorization

    async def start_run(
        self,
        project_id: UUID,
        *,
        actor: Actor,
        idempotency_key: str,
    ) -> dict[str, Any]:
        await self.authorization.require(
            actor,
            project_id,
            roles={ProjectRole.REVIEWER, ProjectRole.OPERATOR, ProjectRole.AGENT},
        )
        request_hash = f"sha256:{sha256(f'{project_id}:start-run'.encode()).hexdigest()}"
        storage_key = f"run:{idempotency_key}"
        async with self.sessions() as session:
            existing = (
                await session.execute(
                    select(idempotency_keys).where(
                        idempotency_keys.c.project_id == project_id,
                        idempotency_keys.c.key == storage_key,
                    )
                )
            ).mappings().one_or_none()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise ValueError("idempotency key was used with a different request")
            return dict(existing["response_body"])

        run_id = await self.workflow.create_incremental_run(project_id=project_id)
        if run_id is None:
            raise ValueError("a workflow run requires at least one parsed source version")
        body = {"id": str(run_id), "project_id": str(project_id), "status": "processing"}
        async with self.sessions.begin() as session:
            await session.execute(
                postgres_insert(idempotency_keys)
                .values(
                    project_id=project_id,
                    key=storage_key,
                    request_hash=request_hash,
                    response_status=202,
                    response_body=body,
                )
                .on_conflict_do_nothing(
                    index_elements=[idempotency_keys.c.project_id, idempotency_keys.c.key]
                )
            )
            stored = (
                await session.execute(
                    select(idempotency_keys).where(
                        idempotency_keys.c.project_id == project_id,
                        idempotency_keys.c.key == storage_key,
                    )
                )
            ).mappings().one()
        if stored["request_hash"] != request_hash:
            raise ValueError("idempotency key was used with a different request")
        return dict(stored["response_body"])

    async def inspect_run(self, run_id: UUID, actor: Actor) -> dict[str, Any]:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(workflow_runs).where(workflow_runs.c.id == run_id)
                )
            ).mappings().one_or_none()
        if row is None:
            raise LookupError("workflow run not found")
        await self.authorization.require(actor, row["project_id"])
        status = row["status"]
        if status in {"pending", "running"}:
            status = "processing"
        return {
            "id": str(run_id),
            "status": status,
            "current_stage": row["current_stage"],
        }


class AuditService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        authorization: ProjectAuthorizer,
    ) -> None:
        self.sessions = sessions
        self.authorization = authorization

    async def list_audit(
        self,
        project_id: UUID,
        actor: Actor,
        *,
        limit: int,
        offset: int,
    ) -> tuple[list[dict[str, Any]], int]:
        async with self.sessions() as session:
            await self.authorization.require(actor, project_id, session=session)
            statement = select(audit_events).where(audit_events.c.project_id == project_id)
            total = int(
                await session.scalar(
                    select(func.count()).select_from(audit_events).where(
                        audit_events.c.project_id == project_id
                    )
                )
                or 0
            )
            rows = (
                await session.execute(
                    statement.order_by(audit_events.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).mappings().all()
        return (
            [
                {
                    "id": str(row["id"]),
                    "event_type": row["event_type"],
                    "actor_id": row["actor_id"],
                    "actor_source": row["actor_source"],
                    "aggregate_type": row["aggregate_type"],
                    "aggregate_id": str(row["aggregate_id"]),
                    "prior_version": row["prior_version"],
                    "new_version": row["new_version"],
                    "command_id": row["command_id"],
                    "decision_id": row["decision_id"],
                    "artifact_hashes": list(row["artifact_hashes"] or []),
                    "payload": row["payload"],
                    "created_at": row["created_at"].isoformat(),
                }
                for row in rows
            ],
            total,
        )
