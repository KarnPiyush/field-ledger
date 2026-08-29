from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fieldledger.config import Settings
from fieldledger.domain.models import DependencyKind, ReviewDecision, ReviewDecisionKind
from fieldledger.domain.review import DecisionSubmission, DependencyEntry, DependencyVector
from fieldledger.persistence.tables import (
    audit_events,
    confirmation_tokens,
    document_versions,
    documents,
    idempotency_keys,
    outbox,
    project_memberships,
    projects,
    provider_jobs,
    provider_sessions,
    review_batches,
    review_commands,
    review_decisions,
    review_dependencies,
    review_items,
    rule_sets,
    workflow_runs,
)
from fieldledger.providers.base import (
    ApprovalRequest,
    ExportedFile,
    ExportRequest,
    ProposedChange,
    ProviderDocument,
    ProviderDocumentState,
    ProviderMutationUnknown,
    ProviderOutcome,
    ProviderOutcomeStatus,
)
from fieldledger.security import (
    Actor,
    ActorSource,
    AuthorizationError,
    ConfirmationTokenError,
    GlobalRole,
    ProjectAuthorizer,
    ProjectRole,
    SecurityManager,
    token_hash,
)
from fieldledger.services.dependency_service import DependencyService
from fieldledger.services.mutation_fence import project_mutation_fence
from fieldledger.services.project_service import AuditService
from fieldledger.services.review_service import (
    IdempotencyConflictError,
    ReviewNotFoundError,
    ReviewService,
)
from sqlalchemy import delete, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio
NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
POLICY_HASH = "sha256:policy-v1"


@dataclass
class FakeProvider:
    outcome: ProviderOutcome = field(
        default_factory=lambda: ProviderOutcome(
            job_id="external-job",
            status=ProviderOutcomeStatus.COMPLETED,
        )
    )
    state: ProviderDocumentState = field(
        default_factory=lambda: ProviderDocumentState(
            document_id="provider-document",
            version="provider-v1",
            content=b"before",
            pending_changes=(
                ProposedChange(id="amount", content={"before": "100", "after": "125"}),
                ProposedChange(id="date", content={"before": "old", "after": "new"}),
            ),
        )
    )
    after_state: ProviderDocumentState | None = None
    mutation_unknown: bool = False
    read_error: bool = False
    read_error_after: int | None = None
    calls: list[ApprovalRequest] = field(default_factory=list)
    read_calls: int = 0
    approve_started: asyncio.Event | None = None
    approve_release: asyncio.Event | None = None

    async def approve(self, request: ApprovalRequest) -> ProviderOutcome:
        self.calls.append(request)
        if self.approve_started is not None:
            self.approve_started.set()
        if self.approve_release is not None:
            await self.approve_release.wait()
        if self.mutation_unknown:
            if self.after_state is not None:
                self.state = self.after_state
            raise ProviderMutationUnknown("response lost")
        self.state = self.after_state or self.state.model_copy(
            update={
                "version": "provider-v2",
                "content": b"applied",
                "pending_changes": self.outcome.proposed_changes,
                "last_idempotency_key": request.idempotency_key,
                "last_outcome": self.outcome.status,
            }
        )
        return self.outcome

    async def read_document(self, document_id: str) -> ProviderDocumentState:
        self.read_calls += 1
        if self.read_error or (
            self.read_error_after is not None and self.read_calls >= self.read_error_after
        ):
            raise TimeoutError("post-mutation refresh failed")
        return self.state.model_copy(update={"document_id": document_id})

    async def export(self, request: ExportRequest) -> ExportedFile:
        return ExportedFile(
            name=f"{request.document_id}.{request.format}",
            media_type="application/octet-stream",
            content=self.state.content,
        )

    async def upload(self, source, *, idempotency_key: str) -> ProviderDocument:  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def start_edit(self, request):  # type: ignore[no-untyped-def]
        raise NotImplementedError

    async def poll(self, job_id: str) -> ProviderOutcome:
        raise NotImplementedError


@dataclass(frozen=True)
class ReviewContext:
    engine: AsyncEngine
    sessions: async_sessionmaker
    service: ReviewService
    provider: FakeProvider
    project_id: UUID
    other_project_id: UUID
    batch_id: UUID
    item_ids: tuple[UUID, UUID]
    source_document_id: UUID
    source_version_id: UUID
    provider_session_id: UUID


def actor(
    project_id: UUID,
    actor_id: str = "reviewer-1",
    *,
    source: ActorSource = ActorSource.UI,
    role: ProjectRole = ProjectRole.REVIEWER,
) -> Actor:
    return Actor(
        actor_id=actor_id,
        source=source,
        global_role=(
            GlobalRole.OPERATOR if role is ProjectRole.OPERATOR else GlobalRole.USER
        ),
    )


@pytest.fixture
async def review_context() -> AsyncIterator[ReviewContext]:
    engine = create_async_engine(Settings.from_environment().database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    provider = FakeProvider()
    security = SecurityManager(
        signing_secret="placeholder-confirmation-test-secret",
        issuer="fieldledger-test",
        audience="fieldledger-test-client",
        local_demo_enabled=True,
        demo_bootstrap_secret="placeholder-demo-bootstrap",
        demo_identities={
            "reviewer": {
                "actor_id": "reviewer-1",
                "source": "ui",
                "global_role": "user",
            }
        },
    )
    authorization = ProjectAuthorizer(sessions)
    dependencies = DependencyService(sessions, policy_content_hash=POLICY_HASH)
    service = ReviewService(
        sessions,
        provider=provider,
        security=security,
        authorization=authorization,
        dependencies=dependencies,
    )

    project_id, other_project_id = uuid4(), uuid4()
    run_id, target_document_id, target_version_id = uuid4(), uuid4(), uuid4()
    source_document_id, source_version_id, rule_set_id = uuid4(), uuid4(), uuid4()
    provider_session_id, provider_job_id, batch_id = uuid4(), uuid4(), uuid4()
    item_ids = (uuid4(), uuid4())
    proposal_hash = "sha256:proposal-v1"
    vector = DependencyVector(
        entries=(
            DependencyEntry(
                kind=DependencyKind.TARGET_DOCUMENT_VERSION,
                key=str(target_document_id),
                content_hash="sha256:target-v1",
            ),
            DependencyEntry(
                kind=DependencyKind.SOURCE_VERSION,
                key=str(source_document_id),
                content_hash="sha256:source-v1",
            ),
            DependencyEntry(
                kind=DependencyKind.RULE_SET_VERSION,
                key=str(project_id),
                content_hash="sha256:rules-v1",
            ),
            DependencyEntry(
                kind=DependencyKind.POLICY_VERSION,
                key="fieldledger-policy",
                content_hash=POLICY_HASH,
            ),
            DependencyEntry(
                kind=DependencyKind.PROPOSAL,
                key=str(batch_id),
                content_hash=proposal_hash,
            ),
        )
    )

    async with engine.begin() as connection:
        version = int(await connection.scalar(text("SHOW server_version_num")) or 0)
        assert 170000 <= version < 180000
        await connection.execute(
            insert(projects),
            [
                {"id": project_id, "name": "Review project"},
                {"id": other_project_id, "name": "Other project"},
            ],
        )
        await connection.execute(
            insert(project_memberships),
            [
                {
                    "project_id": project_id,
                    "actor_id": actor_id,
                    "role": ProjectRole.REVIEWER.value,
                    "granted_by": "server-test-fixture",
                }
                for actor_id in ("reviewer-1", "reviewer-2", "proposal-author")
            ]
            + [
                {
                    "project_id": other_project_id,
                    "actor_id": "other-reviewer",
                    "role": ProjectRole.REVIEWER.value,
                    "granted_by": "server-test-fixture",
                }
                ,
                {
                    "project_id": project_id,
                    "actor_id": "mcp-relay",
                    "role": ProjectRole.AGENT.value,
                    "granted_by": "server-test-fixture",
                },
            ],
        )
        await connection.execute(
            insert(documents),
            [
                {
                    "id": target_document_id,
                    "project_id": project_id,
                    "name": "target.docx",
                    "media_type": (
                        "application/vnd.openxmlformats-officedocument."
                        "wordprocessingml.document"
                    ),
                    "fingerprint": "sha256:target-document",
                },
                {
                    "id": source_document_id,
                    "project_id": project_id,
                    "name": "source.txt",
                    "media_type": "text/plain",
                    "fingerprint": "sha256:source-document",
                },
            ],
        )
        await connection.execute(
            insert(document_versions),
            [
                {
                    "id": target_version_id,
                    "document_id": target_document_id,
                    "version_number": 1,
                    "content_hash": "sha256:target-v1",
                    "storage_uri": "postgresql://target/1",
                    "byte_size": 6,
                    "raw_content": b"target",
                },
                {
                    "id": source_version_id,
                    "document_id": source_document_id,
                    "version_number": 1,
                    "content_hash": "sha256:source-v1",
                    "storage_uri": "postgresql://source/1",
                    "byte_size": 6,
                    "raw_content": b"source",
                },
            ],
        )
        await connection.execute(
            insert(rule_sets).values(
                id=rule_set_id,
                project_id=project_id,
                version_number=1,
                version="v1",
                content_hash="sha256:rules-v1",
                raw_content=b"{}",
                rules=[],
            )
        )
        await connection.execute(
            insert(workflow_runs).values(
                id=run_id,
                project_id=project_id,
                status="review_ready",
                input_hash="sha256:run-input",
            )
        )
        await connection.execute(
            insert(provider_sessions).values(
                id=provider_session_id,
                project_id=project_id,
                document_id=target_document_id,
                provider="deterministic",
                external_session_id="provider-session",
                external_document_id="provider-document",
                fence_token=0,
                status="active",
                last_document_version="provider-v1",
                last_pending_change_ids=["amount", "date"],
            )
        )
        await connection.execute(
            insert(provider_jobs).values(
                id=provider_job_id,
                project_id=project_id,
                document_id=target_document_id,
                provider="deterministic",
                external_id="external-job",
                operation="edit",
                status="review_ready",
                request={},
            )
        )
        await connection.execute(
            insert(review_batches).values(
                id=batch_id,
                project_id=project_id,
                workflow_run_id=run_id,
                provider_session_id=provider_session_id,
                provider_job_id=provider_job_id,
                author_actor_id="proposal-author",
                status="open",
                proposal_hash=proposal_hash,
                dependency_digest=vector.digest,
            )
        )
        await connection.execute(
            insert(review_items),
            [
                {
                    "id": item_ids[0],
                    "review_batch_id": batch_id,
                    "item_key": "amount",
                    "title": "Amount",
                    "before_text": "100",
                    "after_text": "125",
                    "reason": "change order",
                },
                {
                    "id": item_ids[1],
                    "review_batch_id": batch_id,
                    "item_key": "date",
                    "title": "Date",
                    "before_text": "2027-01-01",
                    "after_text": "2027-03-15",
                    "reason": "approved extension",
                },
            ],
        )
        await connection.execute(
            insert(review_dependencies),
            [
                {
                    "review_batch_id": batch_id,
                    "kind": entry.kind.value,
                    "dependency_key": entry.key,
                    "content_hash": entry.content_hash,
                }
                for entry in vector.entries
            ],
        )

    try:
        yield ReviewContext(
            engine=engine,
            sessions=sessions,
            service=service,
            provider=provider,
            project_id=project_id,
            other_project_id=other_project_id,
            batch_id=batch_id,
            item_ids=item_ids,
            source_document_id=source_document_id,
            source_version_id=source_version_id,
            provider_session_id=provider_session_id,
        )
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(outbox).where(
                    outbox.c.aggregate_id.in_([batch_id, provider_job_id, provider_session_id])
                )
            )
            await connection.execute(
                delete(audit_events).where(
                    audit_events.c.project_id.in_([project_id, other_project_id])
                )
            )
            await connection.execute(delete(review_batches).where(review_batches.c.id == batch_id))
            await connection.execute(
                delete(projects).where(projects.c.id.in_([project_id, other_project_id]))
            )
        await engine.dispose()


async def _token(
    context: ReviewContext,
    *,
    actor_id: str = "reviewer-1",
    now: datetime = NOW,
) -> str:
    result = await context.service.request_confirmation(
        context.batch_id,
        actor(context.project_id, actor_id),
        now=now,
        lifetime=timedelta(minutes=5),
    )
    return result.token


def _submission(
    context: ReviewContext,
    token: str,
    *,
    key: str = "decision-1",
) -> DecisionSubmission:
    return DecisionSubmission(
        idempotency_key=key,
        confirmation_token=token,
        decisions=(
            ReviewDecision(
                item_id=context.item_ids[0],
                decision=ReviewDecisionKind.APPROVE,
            ),
            ReviewDecision(
                item_id=context.item_ids[1],
                decision=ReviewDecisionKind.REJECT,
                note="date evidence is incomplete",
            ),
        ),
    )


async def test_confirmation_is_hash_only_one_time_actor_batch_digest_bound(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    async with review_context.sessions() as session:
        row = (
            await session.execute(
                select(confirmation_tokens).where(
                    confirmation_tokens.c.review_batch_id == review_context.batch_id
                )
            )
        ).mappings().one()
        assert row["token_hash"].startswith("sha256:")
        assert token not in row["token_hash"]
        assert row["nonce"]
        assert row["actor_id"] == "reviewer-1"
        assert row["review_batch_id"] == review_context.batch_id

    with pytest.raises(ConfirmationTokenError, match="actor"):
        await review_context.service.submit_decisions(
            review_context.batch_id,
            _submission(review_context, token),
            actor(review_context.project_id, "reviewer-2"),
            now=NOW,
        )
    assert review_context.provider.calls == []

    expired = await _token(review_context, now=NOW - timedelta(minutes=10))
    with pytest.raises(ConfirmationTokenError, match="expired"):
        await review_context.service.submit_decisions(
            review_context.batch_id,
            _submission(review_context, expired, key="expired"),
            actor(review_context.project_id),
            now=NOW,
        )
    assert review_context.provider.calls == []


async def test_cross_project_is_403_absent_is_404_and_self_approval_is_denied(
    review_context: ReviewContext,
) -> None:
    with pytest.raises(AuthorizationError) as cross_project:
        await review_context.service.get_batch(
            review_context.batch_id,
            actor(review_context.other_project_id, "other-reviewer"),
        )
    assert cross_project.value.status == 403

    with pytest.raises(ReviewNotFoundError) as absent:
        await review_context.service.get_batch(uuid4(), actor(review_context.project_id))
    assert absent.value.status == 404

    with pytest.raises(AuthorizationError, match="self-approval"):
        await review_context.service.request_confirmation(
            review_context.batch_id,
            actor(review_context.project_id, "proposal-author"),
            now=NOW,
        )


async def test_mixed_decisions_apply_atomically_and_exact_replay_is_side_effect_free(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    command = _submission(review_context, token)
    result, replay = await asyncio.gather(
        review_context.service.submit_decisions(
            review_context.batch_id,
            command,
            actor(review_context.project_id),
            now=NOW,
        ),
        review_context.service.submit_decisions(
            review_context.batch_id,
            command,
            actor(review_context.project_id),
            now=NOW,
        ),
    )

    assert {result.status, replay.status} == {"completed"}
    assert {result.replayed, replay.replayed} == {False, True}
    assert len(review_context.provider.calls) == 1
    assert review_context.provider.read_calls == 2
    assert review_context.provider.calls[0].approved is False
    assert [item.approved for item in review_context.provider.calls[0].changes] == [True, False]
    assert [item.change_id for item in review_context.provider.calls[0].changes] == [
        "amount",
        "date",
    ]

    async with review_context.sessions() as session:
        assert await session.scalar(
            select(func.count()).select_from(review_decisions).where(
                review_decisions.c.review_item_id.in_(review_context.item_ids)
            )
        ) == 2
        assert await session.scalar(
            select(func.count()).select_from(idempotency_keys).where(
                idempotency_keys.c.project_id == review_context.project_id
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(audit_events).where(
                audit_events.c.project_id == review_context.project_id,
                audit_events.c.event_type == "review.decisions_applied",
            )
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(outbox).where(
                outbox.c.aggregate_id == review_context.batch_id
            )
        ) == 1
        assert await session.scalar(
            select(provider_sessions.c.fence_token).where(
                provider_sessions.c.id == review_context.provider_session_id
            )
        ) == 1
        assert await session.scalar(
            select(confirmation_tokens.c.consumed_at).where(
                confirmation_tokens.c.review_batch_id == review_context.batch_id,
                confirmation_tokens.c.actor_id == "reviewer-1",
            ).order_by(confirmation_tokens.c.created_at).limit(1)
        ) is not None

    changed = command.model_copy(
        update={
            "decisions": (
                ReviewDecision(
                    item_id=review_context.item_ids[0],
                    decision=ReviewDecisionKind.REJECT,
                    note="different body",
                ),
                command.decisions[1],
            )
        }
    )
    with pytest.raises(IdempotencyConflictError):
        await review_context.service.submit_decisions(
            review_context.batch_id,
            changed,
            actor(review_context.project_id),
            now=NOW,
        )
    assert len(review_context.provider.calls) == 1


async def test_stolen_token_replay_is_authenticated_before_idempotent_response(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    command = _submission(review_context, token, key="stolen-replay")
    await review_context.service.submit_decisions(
        review_context.batch_id,
        command,
        actor(review_context.project_id),
        now=NOW,
    )

    with pytest.raises(ConfirmationTokenError, match="actor"):
        await review_context.service.submit_decisions(
            review_context.batch_id,
            command,
            actor(review_context.project_id, "reviewer-2"),
            now=NOW,
        )

    assert len(review_context.provider.calls) == 1


async def test_mcp_member_can_relay_but_not_mint_ui_confirmation(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    relay = actor(
        review_context.project_id,
        "mcp-relay",
        source=ActorSource.MCP,
        role=ProjectRole.AGENT,
    )

    with pytest.raises(AuthorizationError):
        await review_context.service.request_confirmation(
            review_context.batch_id, relay, now=NOW
        )

    result = await review_context.service.submit_decisions(
        review_context.batch_id,
        _submission(review_context, token, key="mcp-relay-valid"),
        relay,
        now=NOW,
    )

    assert result.status == "completed"
    async with review_context.sessions() as session:
        command = (
            await session.execute(
                select(review_commands).where(review_commands.c.key == "mcp-relay-valid")
            )
        ).mappings().one()
        assert command["confirmed_actor_id"] == "reviewer-1"
        assert command["relay_actor_id"] == "mcp-relay"
        audit = (
            await session.execute(
                select(audit_events).where(
                    audit_events.c.project_id == review_context.project_id,
                    audit_events.c.event_type == "review.decisions_applied",
                )
            )
        ).mappings().one()
        assert audit["actor_id"] == "reviewer-1"
        assert audit["payload"]["relay_actor_id"] == "mcp-relay"


async def test_project_authorization_comes_from_durable_membership(
    review_context: ReviewContext,
) -> None:
    authorized = ProjectAuthorizer(review_context.sessions)

    assert await authorized.require(
        actor(review_context.project_id), review_context.project_id
    ) is ProjectRole.REVIEWER
    with pytest.raises(AuthorizationError):
        await authorized.require(
            actor(review_context.project_id, "other-reviewer"),
            review_context.project_id,
        )


async def _clone_batch(context: ReviewContext) -> tuple[UUID, tuple[UUID, UUID]]:
    session_id, job_id, batch_id = uuid4(), uuid4(), uuid4()
    item_ids = (uuid4(), uuid4())
    async with context.engine.begin() as connection:
        original_session = (
            await connection.execute(
                select(provider_sessions).where(
                    provider_sessions.c.id == context.provider_session_id
                )
            )
        ).mappings().one()
        original_batch = (
            await connection.execute(
                select(review_batches).where(review_batches.c.id == context.batch_id)
            )
        ).mappings().one()
        dependencies = (
            await connection.execute(
                select(review_dependencies).where(
                    review_dependencies.c.review_batch_id == context.batch_id
                )
            )
        ).mappings().all()
        proposal_hash = f"sha256:proposal-{batch_id}"
        cloned_dependencies = [
            {
                "review_batch_id": batch_id,
                "kind": row["kind"],
                "dependency_key": (
                    str(batch_id)
                    if row["kind"] == DependencyKind.PROPOSAL.value
                    else row["dependency_key"]
                ),
                "content_hash": (
                    proposal_hash
                    if row["kind"] == DependencyKind.PROPOSAL.value
                    else row["content_hash"]
                ),
            }
            for row in dependencies
        ]
        cloned_vector = DependencyVector(
            entries=tuple(
                DependencyEntry(
                    kind=DependencyKind(row["kind"]),
                    key=row["dependency_key"],
                    content_hash=row["content_hash"],
                )
                for row in cloned_dependencies
            )
        )
        await connection.execute(
            insert(provider_sessions).values(
                id=session_id,
                project_id=context.project_id,
                document_id=original_session["document_id"],
                provider="deterministic",
                external_session_id=f"session-{session_id}",
                external_document_id=f"provider-document-{session_id}",
                status="active",
                last_document_version="provider-v1",
            )
        )
        await connection.execute(
            insert(provider_jobs).values(
                id=job_id,
                project_id=context.project_id,
                document_id=original_session["document_id"],
                provider_session_id=session_id,
                provider="deterministic",
                external_id=f"job-{job_id}",
                operation="edit",
                status="review_ready",
                request={},
            )
        )
        await connection.execute(
            insert(review_batches).values(
                id=batch_id,
                project_id=context.project_id,
                workflow_run_id=original_batch["workflow_run_id"],
                provider_session_id=session_id,
                provider_job_id=job_id,
                author_actor_id="proposal-author",
                status="open",
                proposal_hash=proposal_hash,
                dependency_digest=cloned_vector.digest,
            )
        )
        await connection.execute(
            insert(review_items),
            [
                {
                    "id": item_ids[0],
                    "review_batch_id": batch_id,
                    "item_key": "amount",
                    "title": "Amount",
                    "before_text": "100",
                    "after_text": "125",
                    "reason": "change order",
                },
                {
                    "id": item_ids[1],
                    "review_batch_id": batch_id,
                    "item_key": "date",
                    "title": "Date",
                    "before_text": "2027-01-01",
                    "after_text": "2027-03-15",
                    "reason": "extension",
                },
            ],
        )
        await connection.execute(
            insert(review_dependencies),
            cloned_dependencies,
        )
    return batch_id, item_ids


async def test_same_project_key_across_provider_sessions_calls_upstream_once(
    review_context: ReviewContext,
) -> None:
    second_batch, second_items = await _clone_batch(review_context)
    first_token = await _token(review_context)
    second_token = (
        await review_context.service.request_confirmation(
            second_batch,
            actor(review_context.project_id),
            now=NOW,
            lifetime=timedelta(minutes=5),
        )
    ).token
    first = _submission(review_context, first_token, key="cross-session-key")
    second = DecisionSubmission(
        idempotency_key="cross-session-key",
        confirmation_token=second_token,
        decisions=(
            ReviewDecision(item_id=second_items[0], decision=ReviewDecisionKind.APPROVE),
            ReviewDecision(
                item_id=second_items[1],
                decision=ReviewDecisionKind.REJECT,
                note="different provider document",
            ),
        ),
    )

    results = await asyncio.gather(
        review_context.service.submit_decisions(
            review_context.batch_id, first, actor(review_context.project_id), now=NOW
        ),
        review_context.service.submit_decisions(
            second_batch, second, actor(review_context.project_id), now=NOW
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, IdempotencyConflictError) for result in results) == 1
    assert len(review_context.provider.calls) == 1
    async with review_context.sessions() as session:
        assert await session.scalar(
            select(func.count()).select_from(review_commands).where(
                review_commands.c.project_id == review_context.project_id,
                review_commands.c.key == "cross-session-key",
            )
        ) == 1


async def test_expired_command_owner_is_recovered_without_duplicate_mutation(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    command = _submission(review_context, token, key="lost-owner")
    request_hash = review_context.service._request_hash(review_context.batch_id, command)
    async with review_context.engine.begin() as connection:
        await connection.execute(
            insert(review_commands).values(
                project_id=review_context.project_id,
                review_batch_id=review_context.batch_id,
                key="lost-owner",
                request_hash=request_hash,
                confirmed_actor_id="reviewer-1",
                relay_actor_id="reviewer-1",
                confirmation_token_hash=token_hash(token),
                status="applying",
                owner_token="dead-owner",
                lease_expires_at=NOW - timedelta(seconds=1),
            )
        )

    result = await asyncio.wait_for(
        review_context.service.submit_decisions(
            review_context.batch_id,
            command,
            actor(review_context.project_id),
            now=NOW,
        ),
        timeout=5,
    )

    assert result.status == "completed"
    assert len(review_context.provider.calls) == 1
    async with review_context.sessions() as session:
        row = (
            await session.execute(
                select(review_commands).where(review_commands.c.key == "lost-owner")
            )
        ).mappings().one()
        assert row["status"] == "completed"
        assert row["owner_token"] is None
        assert row["attempts"] == 1


async def test_project_mutation_fence_blocks_dependency_change_during_provider_call(
    review_context: ReviewContext,
) -> None:
    review_context.provider.approve_started = asyncio.Event()
    review_context.provider.approve_release = asyncio.Event()
    token = await _token(review_context)
    mutation_acquired = asyncio.Event()

    submit = asyncio.create_task(
        review_context.service.submit_decisions(
            review_context.batch_id,
            _submission(review_context, token, key="project-fence"),
            actor(review_context.project_id),
            now=NOW,
        )
    )
    await asyncio.wait_for(review_context.provider.approve_started.wait(), timeout=5)

    async def mutate_dependency() -> None:
        async with review_context.sessions.begin() as session:
            async with project_mutation_fence(session, review_context.project_id):
                mutation_acquired.set()
                await session.execute(
                    insert(document_versions).values(
                        id=uuid4(),
                        document_id=review_context.source_document_id,
                        version_number=2,
                        content_hash="sha256:source-after-provider",
                        storage_uri="postgresql://source/fenced",
                        byte_size=6,
                        raw_content=b"source",
                    )
                )

    mutation = asyncio.create_task(mutate_dependency())
    await asyncio.sleep(0.15)
    assert not mutation_acquired.is_set()
    review_context.provider.approve_release.set()
    assert (await asyncio.wait_for(submit, timeout=5)).status == "completed"
    await asyncio.wait_for(mutation, timeout=5)
    assert len(review_context.provider.calls) == 1


async def test_out_of_band_provider_version_is_quarantined_before_mutation(
    review_context: ReviewContext,
) -> None:
    review_context.provider.state = review_context.provider.state.model_copy(
        update={
            "version": "provider-out-of-band-v9",
            "content": b"changed elsewhere",
            "unexpected_events": ({"id": "external-event", "type": "insert"},),
        }
    )
    token = await _token(review_context)

    result = await review_context.service.submit_decisions(
        review_context.batch_id,
        _submission(review_context, token, key="out-of-band"),
        actor(review_context.project_id),
        now=NOW,
    )

    assert result.status == "reconciliation_required"
    assert review_context.provider.calls == []
    async with review_context.sessions() as session:
        row = (
            await session.execute(
                select(provider_sessions).where(
                    provider_sessions.c.id == review_context.provider_session_id
                )
            )
        ).mappings().one()
        assert row["status"] == "quarantined"
        assert row["last_document_version"] == "provider-v1"


async def test_post_mutation_version_mismatch_quarantines_confirmed_response(
    review_context: ReviewContext,
) -> None:
    review_context.provider.outcome = ProviderOutcome(
        job_id="external-job",
        status=ProviderOutcomeStatus.COMPLETED,
        document=ProviderDocument(
            id="provider-document",
            version="provider-expected-v2",
            content=b"expected",
        ),
    )
    review_context.provider.after_state = ProviderDocumentState(
        document_id="provider-document",
        version="provider-out-of-band-v3",
        content=b"unexpected",
        last_idempotency_key="post-version-mismatch",
        last_outcome=ProviderOutcomeStatus.COMPLETED,
    )
    token = await _token(review_context)

    result = await review_context.service.submit_decisions(
        review_context.batch_id,
        _submission(review_context, token, key="post-version-mismatch"),
        actor(review_context.project_id),
        now=NOW,
    )

    assert result.status == "reconciliation_required"
    assert len(review_context.provider.calls) == 1
    async with review_context.sessions() as session:
        row = (
            await session.execute(
                select(provider_sessions).where(
                    provider_sessions.c.id == review_context.provider_session_id
                )
            )
        ).mappings().one()
        assert row["status"] == "quarantined"
        assert row["last_document_version"] == "provider-v1"


async def test_indirect_dependency_movement_returns_exact_stale_delta_before_provider(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    async with review_context.engine.begin() as connection:
        await connection.execute(
            insert(document_versions).values(
                id=uuid4(),
                document_id=review_context.source_document_id,
                version_number=2,
                content_hash="sha256:source-v2",
                storage_uri="postgresql://source/2",
                byte_size=7,
                raw_content=b"source2",
            )
        )

    result = await review_context.service.submit_decisions(
        review_context.batch_id,
        _submission(review_context, token),
        actor(review_context.project_id),
        now=NOW,
    )

    assert result.status == "stale"
    assert [change.model_dump(mode="json") for change in result.stale_changes] == [
        {
            "kind": "source_version",
            "key": str(review_context.source_document_id),
            "reviewed_hash": "sha256:source-v1",
            "current_hash": "sha256:source-v2",
        }
    ]
    assert review_context.provider.calls == []


@pytest.mark.parametrize(
    ("last_key", "last_status", "expected"),
    [
        ("lost-1", ProviderOutcomeStatus.COMPLETED, "completed"),
        (None, None, "reconciliation_required"),
    ],
)
async def test_lost_response_reads_authoritative_state_or_retains_ambiguous_decisions(
    review_context: ReviewContext,
    last_key: str | None,
    last_status: ProviderOutcomeStatus | None,
    expected: str,
) -> None:
    review_context.provider.mutation_unknown = True
    review_context.provider.after_state = ProviderDocumentState(
        document_id="provider-document",
        version="provider-v2",
        content=b"possibly applied",
        last_idempotency_key=last_key,
        last_outcome=last_status,
    )
    token = await _token(review_context)

    result = await review_context.service.submit_decisions(
        review_context.batch_id,
        _submission(review_context, token, key="lost-1"),
        actor(review_context.project_id),
        now=NOW,
    )

    assert result.status == expected
    assert len(review_context.provider.calls) == 1
    async with review_context.sessions() as session:
        batch = (
            await session.execute(
                select(review_batches).where(review_batches.c.id == review_context.batch_id)
            )
        ).mappings().one()
        if expected == "reconciliation_required":
            assert batch["retained_decisions"]
            assert await session.scalar(
                select(func.count()).select_from(review_decisions).where(
                    review_decisions.c.review_item_id.in_(review_context.item_ids)
                )
            ) == 0
        else:
            assert await session.scalar(
                select(func.count()).select_from(review_decisions).where(
                    review_decisions.c.review_item_id.in_(review_context.item_ids)
                )
            ) == 2


async def test_post_mutation_read_failure_is_visible_and_retains_decisions(
    review_context: ReviewContext,
) -> None:
    review_context.provider.read_error_after = 2
    token = await _token(review_context)

    result = await review_context.service.submit_decisions(
        review_context.batch_id,
        _submission(review_context, token, key="refresh-failed"),
        actor(review_context.project_id),
        now=NOW,
    )

    assert result.status == "reconciliation_required"
    assert len(review_context.provider.calls) == 1
    assert review_context.provider.read_calls == 2
    async with review_context.sessions() as session:
        retained = await session.scalar(
            select(review_batches.c.retained_decisions).where(
                review_batches.c.id == review_context.batch_id
            )
        )
        assert len(retained) == 2
        assert await session.scalar(
            select(func.count()).select_from(review_decisions).where(
                review_decisions.c.review_item_id.in_(review_context.item_ids)
            )
        ) == 0


async def test_exact_completed_replay_is_available_after_confirmation_token_expiry(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    command = _submission(review_context, token, key="expiry-safe-replay")
    first = await review_context.service.submit_decisions(
        review_context.batch_id,
        command,
        actor(review_context.project_id),
        now=NOW,
    )

    replay = await review_context.service.submit_decisions(
        review_context.batch_id,
        command,
        actor(review_context.project_id),
        now=NOW + timedelta(minutes=6),
    )

    assert replay == first.model_copy(update={"replayed": True})
    assert len(review_context.provider.calls) == 1


async def test_crash_after_provider_success_reconciles_without_second_mutation(
    review_context: ReviewContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = await _token(review_context)
    command = _submission(review_context, token, key="crash-after-provider")
    original_finalize = review_context.service._finalize
    crashed = False

    async def fail_once(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal crashed
        if not crashed:
            crashed = True
            raise RuntimeError("database commit connection dropped")
        return await original_finalize(*args, **kwargs)

    monkeypatch.setattr(review_context.service, "_finalize", fail_once)
    with pytest.raises(RuntimeError, match="connection dropped"):
        await review_context.service.submit_decisions(
            review_context.batch_id,
            command,
            actor(review_context.project_id),
            now=NOW,
        )
    assert len(review_context.provider.calls) == 1

    result = await review_context.service.submit_decisions(
        review_context.batch_id,
        command,
        actor(review_context.project_id),
        now=NOW + timedelta(seconds=31),
    )

    assert result.status == "completed"
    assert len(review_context.provider.calls) == 1
    async with review_context.sessions() as session:
        session_status = await session.scalar(
            select(provider_sessions.c.status).where(
                provider_sessions.c.id == review_context.provider_session_id
            )
        )
    assert session_status == "active"


async def test_review_audit_exposes_command_decision_and_artifact_provenance(
    review_context: ReviewContext,
) -> None:
    token = await _token(review_context)
    await review_context.service.submit_decisions(
        review_context.batch_id,
        _submission(review_context, token, key="audit-provenance"),
        actor(review_context.project_id),
        now=NOW,
    )

    audit = AuditService(review_context.sessions, ProjectAuthorizer(review_context.sessions))
    items, _total = await audit.list_audit(
        review_context.project_id,
        actor(review_context.project_id),
        limit=100,
        offset=0,
    )
    applied = next(item for item in items if item["event_type"] == "review.decisions_applied")
    assert applied["command_id"]
    assert applied["decision_id"]
    assert "sha256:proposal-v1" in applied["artifact_hashes"]
    assert applied["aggregate_id"] == str(review_context.batch_id)
    assert applied["payload"]["decision_ids"]
