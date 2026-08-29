from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.agent.stages import DeterministicStageExecutor
from fieldledger.domain.models import DependencyKind
from fieldledger.domain.review import DependencyEntry, DependencyVector
from fieldledger.persistence.tables import (
    audit_events,
    citations,
    document_aliases,
    document_versions,
    documents,
    facts,
    idempotency_keys,
    project_memberships,
    projects,
    review_batches,
    review_dependencies,
    review_items,
    source_chunks,
    workflow_runs,
)
from fieldledger.providers.base import DocumentProvider
from fieldledger.providers.deterministic import DeterministicDocumentProvider
from fieldledger.security import Actor, ActorSource, AuthorizationError, ProjectRole
from fieldledger.services.ingestion_service import IngestionService, IngestSource
from fieldledger.services.mutation_fence import acquire_project_mutation_fence
from fieldledger.services.provider_service import ProviderService
from fieldledger.services.workflow_service import WorkflowService
from fieldledger.worker import run_worker_iteration


def _canonical_fixtures() -> tuple[dict[str, Any], dict[str, bytes]]:
    fixture_root = Path(__file__).resolve().parents[5] / "demo" / "fixtures"
    oracle = json.loads((fixture_root / "oracle.json").read_text(encoding="utf-8"))
    fixture_contents: dict[str, bytes] = {}
    for specification in oracle["sources"]:
        name = str(specification["name"])
        content = (fixture_root / name).read_bytes()
        digest = f"sha256:{sha256(content).hexdigest()}"
        if digest != specification["sha256"]:
            raise RuntimeError(f"demo fixture does not match oracle: {name}")
        fixture_contents[name] = content
    return oracle, fixture_contents


class DemoSeedService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        enabled: bool,
        provider: DocumentProvider,
        workflow: WorkflowService,
        ingestion: IngestionService,
        providers: ProviderService,
    ) -> None:
        self.sessions = sessions
        self.enabled = enabled
        self.provider = provider
        self.workflow = workflow
        self.ingestion = ingestion
        self.providers = providers

    async def seed(self, *, actor: Actor, idempotency_key: str) -> dict[str, Any]:
        if not self.enabled or actor.source is not ActorSource.UI:
            raise AuthorizationError("demo seed requires an authenticated local demo actor")
        if not isinstance(self.provider, DeterministicDocumentProvider):
            raise AuthorizationError("demo seed requires the deterministic provider")

        project_id = uuid5(NAMESPACE_URL, f"fieldledger-demo:{actor.actor_id}:project")
        async with self.sessions.begin() as lock_session:
            await lock_session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"demo-seed:{project_id}"},
            )
            return await self._seed_locked(
                actor=actor,
                idempotency_key=idempotency_key,
                project_id=project_id,
            )

    async def _seed_locked(
        self,
        *,
        actor: Actor,
        idempotency_key: str,
        project_id: UUID,
    ) -> dict[str, Any]:

        oracle, fixture_contents = _canonical_fixtures()

        storage_key = f"demo-seed:{idempotency_key}"
        async with self.sessions.begin() as session:
            await acquire_project_mutation_fence(session, project_id)
            await session.execute(
                postgres_insert(projects)
                .values(
                    id=project_id,
                    name=oracle["project"],
                    description=oracle["pile"],
                )
                .on_conflict_do_update(
                    index_elements=[projects.c.id],
                    set_={"name": oracle["project"], "description": oracle["pile"]},
                )
            )
            await session.execute(
                postgres_insert(project_memberships)
                .values(
                    project_id=project_id,
                    actor_id=actor.actor_id,
                    role=ProjectRole.REVIEWER.value,
                    granted_by=actor.actor_id,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        project_memberships.c.project_id,
                        project_memberships.c.actor_id,
                    ]
                )
            )
            existing = (
                await session.execute(
                    select(idempotency_keys).where(
                        idempotency_keys.c.project_id == project_id,
                        idempotency_keys.c.key == storage_key,
                    )
                )
            ).mappings().one_or_none()
            if existing is not None:
                return {**dict(existing["response_body"]), "replayed": True}

        rules = await self.ingestion.store_rule_set(
            project_id=project_id,
            raw_content=fixture_contents["rules.json"],
        )
        if rules.status == "failed":
            raise RuntimeError("canonical demo rules were rejected")

        media_types = {
            ".pdf": "application/pdf",
            ".docx": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            ".txt": "text/plain",
            ".md": "text/markdown",
        }
        document_names = tuple(
            str(item["name"])
            for item in oracle["sources"]
            if item["name"] != "rules.json"
        )
        ingestion = await self.ingestion.ingest_pile(
            project_id=project_id,
            sources=tuple(
                IngestSource(
                    name=name,
                    media_type=media_types[Path(name).suffix],
                    content=fixture_contents[name],
                )
                for name in document_names
            ),
        )
        run_id = ingestion.run_id or await self.workflow.create_incremental_run(
            project_id=project_id
        )
        if run_id is None:
            raise RuntimeError("demo seed did not produce a manifest-bearing workflow run")

        executor = DeterministicStageExecutor(self.sessions)
        async with self.sessions() as session:
            pending_run_ids = tuple(
                await session.scalars(
                    select(workflow_runs.c.id)
                    .where(
                        workflow_runs.c.project_id == project_id,
                        workflow_runs.c.status.in_(("pending", "running")),
                    )
                    .order_by(workflow_runs.c.created_at, workflow_runs.c.id)
                )
            )
        for pending_run_id in pending_run_ids:
            for _ in range(16):
                processed = await run_worker_iteration(
                    self.workflow,
                    worker_id=f"demo-seed:{actor.actor_id}",
                    execute_stage=executor,
                    run_id=pending_run_id,
                )
                if not processed:
                    break
        async with self.sessions() as session:
            run_status = await session.scalar(
                select(workflow_runs.c.status).where(workflow_runs.c.id == run_id)
            )
        if run_status != "review_ready":
            raise RuntimeError("demo workflow did not reach review-ready state")

        target = next(
            (
                item
                for item in ingestion.files
                if item.source_name == "change-order-014.docx"
                and item.document_id is not None
            ),
            None,
        )
        if target is None:
            async with self.sessions() as session:
                document_id = await session.scalar(
                    select(document_aliases.c.document_id)
                    .where(document_aliases.c.source_path == "change-order-014.docx")
                    .where(document_aliases.c.project_id == project_id)
                    .limit(1)
                )
        else:
            document_id = target.document_id
        if document_id is None:
            raise RuntimeError("demo target document was not persisted")

        job = await self.providers.start_edit(
            document_id=UUID(str(document_id)),
            workflow_run_id=run_id,
            prompt="Seed Change order 014 cited review",
            idempotency_key=f"demo-provider-edit-v2:{actor.actor_id}",
            actor=actor,
        )
        polled = await self.providers.poll(UUID(str(job["id"])), actor)
        batch_id = polled.get("review_batch_id")
        if batch_id is None:
            raise RuntimeError("demo provider did not produce a review batch")

        async with self.sessions.begin() as session:
            cited_rows = (
                await session.execute(
                    select(
                        facts.c.fact_key,
                        facts.c.value,
                        facts.c.unit,
                        citations.c.source_chunk_id,
                        citations.c.coordinate,
                        citations.c.quoted_text,
                        source_chunks.c.document_version_id,
                        documents.c.name.label("source_name"),
                    )
                    .select_from(
                        facts.join(citations)
                        .join(source_chunks, source_chunks.c.id == citations.c.source_chunk_id)
                        .join(
                            document_versions,
                            document_versions.c.id == source_chunks.c.document_version_id,
                        )
                        .join(documents, documents.c.id == document_versions.c.document_id)
                    )
                    .where(facts.c.workflow_run_id == run_id)
                    .order_by(facts.c.fact_key, documents.c.name)
                )
            ).mappings().all()
            cited_by_key: dict[str, Any] = {}
            for cited in cited_rows:
                key = str(cited["fact_key"])
                if (
                    key not in cited_by_key
                    or key == "approved_change_value"
                    and cited["source_name"] == "change-order-014.docx"
                ):
                    cited_by_key[key] = cited
            title_keys = {
                "Contract value": "contract_value",
                "Feeder allowance": "approved_change_value",
                "Completion date": "revised_completion_date",
                "Installed quantity": "installed_quantity",
                "Invoice amount": "invoice_amount",
            }
            items = (
                await session.execute(
                    select(review_items)
                    .where(review_items.c.review_batch_id == batch_id)
                    .order_by(review_items.c.title)
                )
            ).mappings().all()
            if len(items) != int(oracle["expected_review_items"]):
                raise RuntimeError("canonical demo provider produced the wrong item count")
            for item in items:
                fact_key = title_keys.get(str(item["title"]))
                selected_citation = cited_by_key.get(fact_key or "")
                if selected_citation is None:
                    raise RuntimeError(f"demo review item has no cited fact: {item['title']}")
                await session.execute(
                    update(review_items)
                    .where(review_items.c.id == item["id"])
                    .values(
                        evidence={
                            "fact_key": selected_citation["fact_key"],
                            "value": selected_citation["value"],
                            "unit": selected_citation["unit"],
                            "source_name": selected_citation["source_name"],
                        },
                        citation={
                            "document_version_id": str(
                                selected_citation["document_version_id"]
                            ),
                            "source_chunk_id": str(selected_citation["source_chunk_id"]),
                            "coordinate": selected_citation["coordinate"],
                            "quoted_text": selected_citation["quoted_text"],
                        },
                    )
                )
            enriched_items = (
                await session.execute(
                    select(review_items)
                    .where(review_items.c.review_batch_id == batch_id)
                    .order_by(review_items.c.item_key)
                )
            ).mappings().all()
            canonical_proposal = json.dumps(
                [
                    {
                        "item_key": item["item_key"],
                        "title": item["title"],
                        "before": item["before_text"],
                        "after": item["after_text"],
                        "reason": item["reason"],
                        "evidence": item["evidence"],
                        "citation": item["citation"],
                    }
                    for item in enriched_items
                ],
                sort_keys=True,
                separators=(",", ":"),
            )
            proposal_hash = f"sha256:{sha256(canonical_proposal.encode()).hexdigest()}"
            await session.execute(
                update(review_dependencies)
                .where(
                    review_dependencies.c.review_batch_id == batch_id,
                    review_dependencies.c.kind == DependencyKind.PROPOSAL.value,
                )
                .values(content_hash=proposal_hash)
            )
            dependency_rows = (
                await session.execute(
                    select(review_dependencies).where(
                        review_dependencies.c.review_batch_id == batch_id
                    )
                )
            ).mappings().all()
            dependency_vector = DependencyVector(
                entries=tuple(
                    DependencyEntry(
                        kind=DependencyKind(row["kind"]),
                        key=row["dependency_key"],
                        content_hash=row["content_hash"],
                    )
                    for row in dependency_rows
                )
            )
            await session.execute(
                update(review_batches)
                .where(review_batches.c.id == batch_id)
                .values(
                    proposal_hash=proposal_hash,
                    dependency_digest=dependency_vector.digest,
                )
            )
            await session.execute(
                update(review_batches)
                .where(
                    review_batches.c.project_id == project_id,
                    review_batches.c.id != batch_id,
                    review_batches.c.status.in_(
                        ("open", "review_ready", "preview_ready", "reconciliation_required")
                    ),
                )
                .values(status="superseded")
            )
            body = {
                "project_id": str(project_id),
                "workflow_run_id": str(run_id),
                "review_batch_id": str(batch_id),
                "document_id": str(document_id),
                "status": "review_ready",
                "replayed": False,
            }
            await session.execute(
                postgres_insert(idempotency_keys)
                .values(
                    project_id=project_id,
                    key=storage_key,
                    request_hash="sha256:demo-seed-v2",
                    response_status=200,
                    response_body=body,
                )
                .on_conflict_do_nothing(
                    index_elements=[idempotency_keys.c.project_id, idempotency_keys.c.key]
                )
            )
            await session.execute(
                insert(audit_events).values(
                    project_id=project_id,
                    actor_id=actor.actor_id,
                    actor_source=actor.source.value,
                    event_type="demo.seeded",
                    aggregate_type="project",
                    aggregate_id=project_id,
                    artifact_hashes=[proposal_hash],
                    payload={
                        "workflow_run_id": str(run_id),
                        "review_batch_id": str(batch_id),
                        "document_id": str(document_id),
                    },
                )
            )
            stored = (
                await session.execute(
                    select(idempotency_keys.c.response_body).where(
                        idempotency_keys.c.project_id == project_id,
                        idempotency_keys.c.key == storage_key,
                    )
                )
            ).scalar_one()
        return dict(stored)
