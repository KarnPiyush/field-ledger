from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, text, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.domain.models import DependencyKind, ReviewDecisionKind
from fieldledger.domain.review import (
    ApplyResult,
    ConfirmationGrant,
    DecisionSubmission,
    DependencyEntry,
    DependencyVector,
)
from fieldledger.persistence.tables import (
    artifact_blobs,
    audit_events,
    confirmation_tokens,
    idempotency_keys,
    outbox,
    provider_jobs,
    provider_session_events,
    provider_sessions,
    review_batches,
    review_commands,
    review_decisions,
    review_dependencies,
    review_items,
    stage_artifacts,
)
from fieldledger.providers.base import (
    ApprovalRequest,
    DocumentProvider,
    ItemApproval,
    ProviderMutationUnknown,
    ProviderOutcome,
    ProviderOutcomeStatus,
)
from fieldledger.security import (
    Actor,
    ActorSource,
    AuthorizationError,
    ConfirmationTokenError,
    ProjectAuthorizer,
    ProjectRole,
    SecurityManager,
    token_hash,
)
from fieldledger.services.dependency_service import DependencyService
from fieldledger.services.mutation_fence import acquire_project_mutation_fence

logger = logging.getLogger(__name__)


class ReviewNotFoundError(LookupError):
    status = 404
    code = "review-not-found"


class IdempotencyConflictError(RuntimeError):
    status = 409
    code = "idempotency-conflict"


class ReviewConflictError(RuntimeError):
    status = 409
    code = "review-conflict"


class ReviewService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        provider: DocumentProvider,
        security: SecurityManager,
        authorization: ProjectAuthorizer,
        dependencies: DependencyService,
    ) -> None:
        self.sessions = sessions
        self.provider = provider
        self.security = security
        self.authorization = authorization
        self.dependencies = dependencies

    async def get_batch(self, batch_id: UUID, actor: Actor) -> dict[str, Any]:
        async with self.sessions() as session:
            batch = await self._batch(session, batch_id)
            await self.authorization.require(actor, batch["project_id"], session=session)
            items = (
                await session.execute(
                    select(review_items)
                    .where(review_items.c.review_batch_id == batch_id)
                    .order_by(review_items.c.created_at, review_items.c.id)
                )
            ).mappings().all()
            return {
                "id": str(batch["id"]),
                "project_id": str(batch["project_id"]),
                "status": batch["status"],
                "dependency_digest": batch["dependency_digest"],
                "items": [self._item_dict(item) for item in items],
            }

    async def list_reviews(
        self,
        project_id: UUID,
        actor: Actor,
        *,
        limit: int,
        offset: int,
        statuses: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        async with self.sessions() as session:
            await self.authorization.require(actor, project_id, session=session)
            statement = select(review_batches).where(review_batches.c.project_id == project_id)
            count_statement = select(func.count()).select_from(review_batches).where(
                review_batches.c.project_id == project_id
            )
            if statuses is not None:
                statement = statement.where(review_batches.c.status.in_(statuses))
                count_statement = count_statement.where(review_batches.c.status.in_(statuses))
            total = int(
                await session.scalar(count_statement)
                or 0
            )
            rows = (
                await session.execute(
                    statement.order_by(review_batches.c.created_at.desc())
                    .limit(limit)
                    .offset(offset)
                )
            ).mappings().all()
            return (
                [
                    {
                        "id": str(row["id"]),
                        "project_id": str(row["project_id"]),
                        "status": row["status"],
                        "dependency_digest": row["dependency_digest"],
                    }
                    for row in rows
                ],
                total,
            )

    async def request_confirmation(
        self,
        batch_id: UUID,
        actor: Actor,
        *,
        now: datetime | None = None,
        lifetime: timedelta | None = None,
    ) -> ConfirmationGrant:
        timestamp = now or datetime.now(UTC)
        token_lifetime = lifetime or timedelta(minutes=5)
        async with self.sessions.begin() as session:
            batch = await self._batch(session, batch_id, lock=True)
            await self.authorization.require(
                actor,
                batch["project_id"],
                roles={ProjectRole.REVIEWER},
                session=session,
            )
            if actor.source is not ActorSource.UI:
                raise AuthorizationError("confirmation must be requested from the UI")
            if batch["author_actor_id"] == actor.actor_id:
                raise AuthorizationError("self-approval is forbidden")
            reviewed = await self.dependencies.reviewed_vector(batch_id, session=session)
            current = await self.dependencies.current_vector(batch_id, session=session)
            if reviewed.digest != batch["dependency_digest"]:
                raise ReviewConflictError("persisted review dependency digest is invalid")
            if reviewed.digest != current.digest:
                raise ReviewConflictError("review dependencies are stale")
            nonce = uuid4().hex
            token = self.security.mint_confirmation(
                actor=actor,
                project_id=batch["project_id"],
                batch_id=batch_id,
                dependency_digest=reviewed.digest,
                nonce=nonce,
                now=timestamp,
                lifetime=token_lifetime,
            )
            expires_at = timestamp + token_lifetime
            await session.execute(
                insert(confirmation_tokens).values(
                    project_id=batch["project_id"],
                    review_batch_id=batch_id,
                    actor_id=actor.actor_id,
                    token_hash=token_hash(token),
                    nonce=nonce,
                    dependency_digest=reviewed.digest,
                    expires_at=expires_at,
                )
            )
        return ConfirmationGrant(
            token=token,
            dependency_digest=reviewed.digest,
            expires_at=expires_at,
        )

    async def submit_decisions(
        self,
        batch_id: UUID,
        command: DecisionSubmission,
        actor: Actor,
        *,
        now: datetime | None = None,
    ) -> ApplyResult:
        timestamp = now or datetime.now(UTC)
        request_hash = self._request_hash(batch_id, command)

        async with self.sessions() as session:
            batch = await self._batch(session, batch_id)
            await self.authorization.require(
                actor,
                batch["project_id"],
                roles=(
                    {ProjectRole.REVIEWER}
                    if actor.source is ActorSource.UI
                    else {ProjectRole.AGENT}
                ),
                session=session,
            )
            replay = await self._idempotent_result(
                session,
                project_id=batch["project_id"],
                key=command.idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                confirmed_actor_id = await session.scalar(
                    select(confirmation_tokens.c.actor_id).where(
                        confirmation_tokens.c.token_hash
                        == token_hash(command.confirmation_token)
                    )
                )
                if (
                    confirmed_actor_id is None
                    or actor.source is ActorSource.UI
                    and confirmed_actor_id != actor.actor_id
                ):
                    raise ConfirmationTokenError("confirmation actor does not match")
                return replay.model_copy(update={"replayed": True})
            claims = self.security.verify_confirmation(
                command.confirmation_token,
                actor_id=(actor.actor_id if actor.source is ActorSource.UI else None),
                project_id=batch["project_id"],
                batch_id=batch_id,
                dependency_digest=batch["dependency_digest"],
                now=timestamp,
            )
            await self.authorization.require_actor_id(
                claims.actor_id,
                batch["project_id"],
                roles={ProjectRole.REVIEWER},
                session=session,
            )
            token_row = (
                await session.execute(
                    select(confirmation_tokens).where(
                        confirmation_tokens.c.token_hash
                        == token_hash(command.confirmation_token)
                    )
                )
            ).mappings().one_or_none()
            self._assert_token_row(
                token_row, claims.nonce, batch, claims.actor_id, timestamp, allow_consumed=True
            )
        owner_token, reserved_replay = await self._reserve_command(
            batch=batch,
            command=command,
            request_hash=request_hash,
            actor=actor,
            confirmed_actor_id=claims.actor_id,
            timestamp=timestamp,
        )
        if reserved_replay is not None:
            return reserved_replay.model_copy(update={"replayed": True})

        async with self.sessions.begin() as session:
            await acquire_project_mutation_fence(session, batch["project_id"])
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": f"provider-mutation:{batch['provider_session_id']}"},
            )
            batch = await self._batch(session, batch_id, lock=True)
            await self.authorization.require(
                actor,
                batch["project_id"],
                roles=(
                    {ProjectRole.REVIEWER}
                    if actor.source is ActorSource.UI
                    else {ProjectRole.AGENT}
                ),
                session=session,
            )
            replay = await self._idempotent_result(
                session,
                project_id=batch["project_id"],
                key=command.idempotency_key,
                request_hash=request_hash,
            )
            if replay is not None:
                return replay.model_copy(update={"replayed": True})
            if owner_token is None:
                raise ReviewConflictError("review command is already applying")
            token_row = (
                await session.execute(
                    select(confirmation_tokens)
                    .where(
                        confirmation_tokens.c.token_hash
                        == token_hash(command.confirmation_token)
                    )
                    .with_for_update()
                )
            ).mappings().one_or_none()
            self._assert_token_row(token_row, claims.nonce, batch, claims.actor_id, timestamp)
            items = (
                await session.execute(
                    select(review_items)
                    .where(review_items.c.review_batch_id == batch_id)
                    .order_by(review_items.c.created_at, review_items.c.id)
                )
            ).mappings().all()
            item_by_id = {item["id"]: item for item in items}
            if set(item_by_id) != {decision.item_id for decision in command.decisions}:
                raise ReviewConflictError("decisions must cover every review item exactly once")

            reviewed = await self.dependencies.reviewed_vector(batch_id, session=session)
            current = await self.dependencies.current_vector(batch_id, session=session)
            if current.digest != reviewed.digest:
                result = ApplyResult(
                    status="stale",
                    batch_id=batch_id,
                    stale_changes=reviewed.diff(current),
                )
                await self._finalize(
                    session,
                    batch=batch,
                    token_row=cast(Any, token_row),
                    actor=actor,
                    confirmed_actor_id=claims.actor_id,
                    command=command,
                    request_hash=request_hash,
                    result=result,
                    timestamp=timestamp,
                    persist_decisions=False,
                    event_type="review.stale",
                )
                return result

            provider_session = (
                await session.execute(
                    select(provider_sessions)
                    .where(provider_sessions.c.id == batch["provider_session_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if provider_session is None or provider_session["status"] not in {
                "active",
                "reconciliation_required",
            }:
                raise ReviewConflictError("provider session is unavailable or quarantined")
            fence_token = int(provider_session["fence_token"]) + 1
            await session.execute(
                update(provider_sessions)
                .where(provider_sessions.c.id == provider_session["id"])
                .values(fence_token=fence_token, updated_at=timestamp)
            )
            job = (
                await session.execute(
                    select(provider_jobs)
                    .where(provider_jobs.c.id == batch["provider_job_id"])
                    .with_for_update()
                )
            ).mappings().one_or_none()
            if job is None or not job["external_id"]:
                raise ReviewConflictError("review batch has no provider job")

            try:
                before_state = await self.provider.read_document(
                    provider_session["external_document_id"]
                )
            except Exception:
                before_state = None
            expected_pending = set(
                provider_session["last_pending_change_ids"]
                or [item["item_key"] for item in items]
            )
            before_events = self._event_ids(
                tuple(provider_session["last_event_ids"] or ())
            )
            observed_before_events = self._event_ids(
                before_state.unexpected_events if before_state is not None else ()
            )
            before_mismatch = (
                before_state is None
                or before_state.version != provider_session["last_document_version"]
                or {change.id for change in before_state.pending_changes} != expected_pending
                or bool(observed_before_events - before_events)
            )
            recovered_outcome = (
                before_state.last_outcome
                if before_state is not None
                and before_state.last_idempotency_key == command.idempotency_key
                else None
            )
            if before_mismatch and recovered_outcome is not None:
                assert before_state is not None
                outcome = ProviderOutcome(
                    job_id=job["external_id"],
                    status=recovered_outcome,
                    proposed_changes=(
                        before_state.pending_changes
                        if recovered_outcome
                        in {
                            ProviderOutcomeStatus.REVIEW_READY,
                            ProviderOutcomeStatus.PREVIEW_READY,
                        }
                        else ()
                    ),
                )
                await session.execute(
                    update(provider_sessions)
                    .where(provider_sessions.c.id == provider_session["id"])
                    .values(
                        status="active",
                        quarantine_reason=None,
                        last_document_version=before_state.version,
                        last_pending_change_ids=sorted(
                            change.id for change in before_state.pending_changes
                        ),
                        last_event_ids=sorted(observed_before_events),
                        updated_at=timestamp,
                    )
                )
                result, next_batch_id = await self._result_for_outcome(
                    session,
                    batch=batch,
                    reviewed=reviewed,
                    outcome=outcome,
                    timestamp=timestamp,
                )
                result = result.model_copy(update={"next_batch_id": next_batch_id})
                await session.execute(
                    update(provider_jobs)
                    .where(provider_jobs.c.id == job["id"])
                    .values(
                        provider_session_id=provider_session["id"],
                        fence_token=fence_token,
                        status=outcome.status.value,
                        result={
                            "proposed_change_ids": [
                                item.id for item in outcome.proposed_changes
                            ],
                            "document_version": before_state.version,
                        },
                        terminal_at=(
                            timestamp
                            if outcome.status
                            in {
                                ProviderOutcomeStatus.COMPLETED,
                                ProviderOutcomeStatus.FAILED,
                                ProviderOutcomeStatus.CANCELLED,
                            }
                            else None
                        ),
                        updated_at=timestamp,
                    )
                )
                await self._finalize(
                    session,
                    batch=batch,
                    token_row=cast(Any, token_row),
                    actor=actor,
                    confirmed_actor_id=claims.actor_id,
                    command=command,
                    request_hash=request_hash,
                    result=result,
                    timestamp=timestamp,
                    persist_decisions=True,
                    event_type="review.decisions_applied",
                )
                return result
            if before_mismatch:
                unexpected = (
                    tuple(
                        event
                        for event in before_state.unexpected_events
                        if self._event_id(event) not in before_events
                    )
                    if before_state is not None
                    else ()
                )
                await self._quarantine_unexpected_events(
                    session, provider_session["id"], unexpected
                )
                await session.execute(
                    update(provider_sessions)
                    .where(provider_sessions.c.id == provider_session["id"])
                    .values(
                        status="quarantined",
                        quarantine_reason="provider state changed outside the project fence",
                        updated_at=timestamp,
                    )
                )
                outcome = ProviderOutcome(
                    job_id=job["external_id"],
                    status=ProviderOutcomeStatus.RECONCILIATION_REQUIRED,
                )
                result, _ = await self._result_for_outcome(
                    session,
                    batch=batch,
                    reviewed=reviewed,
                    outcome=outcome,
                    timestamp=timestamp,
                )
                await self._finalize(
                    session,
                    batch=batch,
                    token_row=cast(Any, token_row),
                    actor=actor,
                    confirmed_actor_id=claims.actor_id,
                    command=command,
                    request_hash=request_hash,
                    result=result,
                    timestamp=timestamp,
                    persist_decisions=False,
                    event_type="review.reconciliation_required",
                )
                return result

            approval = ApprovalRequest(
                job_id=job["external_id"],
                document_id=provider_session["external_document_id"],
                approved=all(
                    decision.decision is ReviewDecisionKind.APPROVE
                    for decision in command.decisions
                ),
                changes=tuple(
                    ItemApproval(
                        change_id=str(item_by_id[decision.item_id]["item_key"]),
                        approved=decision.decision is ReviewDecisionKind.APPROVE,
                        note=decision.note,
                    )
                    for decision in command.decisions
                ),
                idempotency_key=command.idempotency_key,
            )
            try:
                outcome = await self.provider.approve(approval)
                state = await self._read_after_mutation(
                    session,
                    provider_session_id=provider_session["id"],
                    external_document_id=provider_session["external_document_id"],
                )
                if state is None:
                    outcome = ProviderOutcome(
                        job_id=job["external_id"],
                        status=ProviderOutcomeStatus.RECONCILIATION_REQUIRED,
                    )
                elif (
                    state.last_idempotency_key == command.idempotency_key
                    and state.last_outcome is not None
                ):
                    outcome = outcome.model_copy(update={"status": state.last_outcome})
            except ProviderMutationUnknown:
                state = await self._read_after_mutation(
                    session,
                    provider_session_id=provider_session["id"],
                    external_document_id=provider_session["external_document_id"],
                )
                if state is not None and (
                    state.last_idempotency_key == command.idempotency_key
                    and state.last_outcome is not None
                ):
                    outcome = ProviderOutcome(
                        job_id=job["external_id"],
                        status=state.last_outcome,
                    )
                else:
                    outcome = ProviderOutcome(
                        job_id=job["external_id"],
                        status=ProviderOutcomeStatus.RECONCILIATION_REQUIRED,
                    )

            expected_after_pending = (
                {change.id for change in outcome.proposed_changes}
                if outcome.status
                in {ProviderOutcomeStatus.REVIEW_READY, ProviderOutcomeStatus.PREVIEW_READY}
                else set()
            )
            allowed_after_events = before_events | self._event_ids(outcome.incremental_events)
            unexpected_after = (
                tuple(
                    event
                    for event in state.unexpected_events
                    if self._event_id(event) not in allowed_after_events
                )
                if state is not None
                else ()
            )
            after_mismatch = (
                state is None
                or {change.id for change in state.pending_changes} != expected_after_pending
                or bool(unexpected_after)
                or (
                    outcome.document is not None
                    and state.version != outcome.document.version
                )
            )
            if after_mismatch:
                await self._quarantine_unexpected_events(
                    session, provider_session["id"], unexpected_after
                )
                outcome = ProviderOutcome(
                    job_id=job["external_id"],
                    status=ProviderOutcomeStatus.RECONCILIATION_REQUIRED,
                )
            session_status = (
                "quarantined"
                if after_mismatch
                else (
                    "reconciliation_required"
                    if outcome.status is ProviderOutcomeStatus.RECONCILIATION_REQUIRED
                    else "active"
                )
            )
            new_event_ids = sorted(
                allowed_after_events
                | (self._event_ids(state.unexpected_events) if state is not None else set())
            )
            await session.execute(
                update(provider_sessions)
                .where(provider_sessions.c.id == provider_session["id"])
                .values(
                    status=session_status,
                    quarantine_reason=(
                        "provider state did not match mutation evidence"
                        if after_mismatch
                        else None
                    ),
                    last_document_version=(
                        state.version
                        if state is not None and not after_mismatch
                        else provider_session["last_document_version"]
                    ),
                    last_pending_change_ids=(
                        sorted(expected_after_pending)
                        if state is not None and not after_mismatch
                        else provider_session["last_pending_change_ids"]
                    ),
                    last_event_ids=(
                        new_event_ids
                        if state is not None and not after_mismatch
                        else provider_session["last_event_ids"]
                    ),
                    updated_at=timestamp,
                )
            )

            result, next_batch_id = await self._result_for_outcome(
                session,
                batch=batch,
                reviewed=reviewed,
                outcome=outcome,
                timestamp=timestamp,
            )
            result = result.model_copy(update={"next_batch_id": next_batch_id})
            persist_decisions = outcome.status is not ProviderOutcomeStatus.RECONCILIATION_REQUIRED
            await session.execute(
                update(provider_jobs)
                .where(provider_jobs.c.id == job["id"])
                .values(
                    provider_session_id=provider_session["id"],
                    fence_token=fence_token,
                    status=outcome.status.value,
                    result={
                        "proposed_change_ids": [item.id for item in outcome.proposed_changes],
                        "document_version": outcome.document.version if outcome.document else None,
                    },
                    terminal_at=(
                        timestamp
                        if outcome.status
                        in {
                            ProviderOutcomeStatus.COMPLETED,
                            ProviderOutcomeStatus.FAILED,
                            ProviderOutcomeStatus.CANCELLED,
                        }
                        else None
                    ),
                    updated_at=timestamp,
                )
            )
            await self._finalize(
                session,
                batch=batch,
                token_row=cast(Any, token_row),
                actor=actor,
                confirmed_actor_id=claims.actor_id,
                command=command,
                request_hash=request_hash,
                result=result,
                timestamp=timestamp,
                persist_decisions=persist_decisions,
                event_type=(
                    "review.reconciliation_required"
                    if not persist_decisions
                    else "review.decisions_applied"
                ),
            )
            return result

    async def _result_for_outcome(
        self,
        session: AsyncSession,
        *,
        batch: Any,
        reviewed: DependencyVector,
        outcome: ProviderOutcome,
        timestamp: datetime,
    ) -> tuple[ApplyResult, UUID | None]:
        status = outcome.status.value
        next_batch_id: UUID | None = None
        if outcome.status in {
            ProviderOutcomeStatus.REVIEW_READY,
            ProviderOutcomeStatus.PREVIEW_READY,
        } and outcome.proposed_changes:
            next_batch_id = await self._create_successor_batch(
                session,
                batch=batch,
                reviewed=reviewed,
                outcome=outcome,
                timestamp=timestamp,
            )
        return (
            ApplyResult(
                status=cast(Any, status),
                batch_id=batch["id"],
                provider_job_id=batch["provider_job_id"],
            ),
            next_batch_id,
        )

    async def _create_successor_batch(
        self,
        session: AsyncSession,
        *,
        batch: Any,
        reviewed: DependencyVector,
        outcome: ProviderOutcome,
        timestamp: datetime,
    ) -> UUID:
        successor_id = uuid4()
        proposal_json = json.dumps(
            [item.model_dump(mode="json") for item in outcome.proposed_changes],
            sort_keys=True,
            separators=(",", ":"),
        )
        proposal_hash = f"sha256:{sha256(proposal_json.encode()).hexdigest()}"
        entries = tuple(
            entry
            for entry in reviewed.entries
            if entry.kind is not DependencyKind.PROPOSAL
        ) + (
            DependencyEntry(
                kind=DependencyKind.PROPOSAL,
                key=str(successor_id),
                content_hash=proposal_hash,
            ),
        )
        vector = DependencyVector(entries=entries)
        await session.execute(
            insert(review_batches).values(
                id=successor_id,
                project_id=batch["project_id"],
                workflow_run_id=batch["workflow_run_id"],
                supersedes_batch_id=batch["id"],
                provider_session_id=batch["provider_session_id"],
                provider_job_id=batch["provider_job_id"],
                author_actor_id="provider",
                proposal_hash=proposal_hash,
                status="open",
                dependency_digest=vector.digest,
                created_at=timestamp,
            )
        )
        await session.execute(
            insert(review_items),
            [
                {
                    "review_batch_id": successor_id,
                    "item_key": change.id,
                    "title": str(change.content.get("title") or change.id),
                    "before_text": str(change.content.get("before") or ""),
                    "after_text": str(change.content.get("after") or ""),
                    "reason": str(change.content.get("reason") or "provider revision"),
                    "evidence": {},
                    "citation": None,
                    "created_at": timestamp,
                }
                for change in outcome.proposed_changes
            ],
        )
        await session.execute(
            insert(review_dependencies),
            [
                {
                    "review_batch_id": successor_id,
                    "kind": entry.kind.value,
                    "dependency_key": entry.key,
                    "content_hash": entry.content_hash,
                    "created_at": timestamp,
                }
                for entry in vector.entries
            ],
        )
        return successor_id

    async def _finalize(
        self,
        session: AsyncSession,
        *,
        batch: Any,
        token_row: Any,
        actor: Actor,
        confirmed_actor_id: str,
        command: DecisionSubmission,
        request_hash: str,
        result: ApplyResult,
        timestamp: datetime,
        persist_decisions: bool,
        event_type: str,
    ) -> None:
        confirmation_hash = token_row["token_hash"]
        decision_ids: list[UUID] = []
        if persist_decisions:
            for decision in command.decisions:
                inserted = (
                    await session.execute(
                        postgres_insert(review_decisions)
                        .values(
                            review_item_id=decision.item_id,
                            actor_id=confirmed_actor_id,
                            decision=decision.decision.value,
                            note=decision.note,
                            confirmation_token_hash=confirmation_hash,
                            created_at=timestamp,
                        )
                        .on_conflict_do_nothing(index_elements=[review_decisions.c.review_item_id])
                        .returning(review_decisions.c.id)
                    )
                ).scalar_one_or_none()
                if inserted is None:
                    raise ReviewConflictError("review item was already decided")
                decision_ids.append(cast(UUID, inserted))
                await session.execute(
                    update(review_items)
                    .where(review_items.c.id == decision.item_id)
                    .values(status=decision.decision.value)
                )
        retained = (
            []
            if persist_decisions
            else [decision.model_dump(mode="json") for decision in command.decisions]
        )
        await session.execute(
            update(review_batches)
            .where(review_batches.c.id == batch["id"])
            .values(status=result.status, retained_decisions=retained)
        )
        await session.execute(
            update(confirmation_tokens)
            .where(confirmation_tokens.c.id == token_row["id"])
            .values(consumed_at=timestamp)
        )
        body = result.model_dump(mode="json")
        await session.execute(
            insert(idempotency_keys).values(
                project_id=batch["project_id"],
                key=command.idempotency_key,
                request_hash=request_hash,
                response_status=200,
                response_body=body,
            )
        )
        await session.execute(
            update(review_commands)
            .where(
                review_commands.c.project_id == batch["project_id"],
                review_commands.c.key == command.idempotency_key,
                review_commands.c.request_hash == request_hash,
            )
            .values(
                status=(
                    "reconciliation_required"
                    if result.status == "reconciliation_required"
                    else "completed"
                ),
                owner_token=None,
                lease_expires_at=None,
                response_status=200,
                response_body=body,
                updated_at=timestamp,
            )
        )
        await session.execute(
            insert(audit_events).values(
                project_id=batch["project_id"],
                actor_id=confirmed_actor_id,
                actor_source=ActorSource.UI.value,
                event_type=event_type,
                aggregate_type="review_batch",
                aggregate_id=batch["id"],
                command_id=await session.scalar(
                    select(review_commands.c.id).where(
                        review_commands.c.project_id == batch["project_id"],
                        review_commands.c.key == command.idempotency_key,
                        review_commands.c.request_hash == request_hash,
                    )
                ),
                decision_id=(decision_ids[0] if decision_ids else None),
                artifact_hashes=sorted(
                    {
                        *(
                            [batch["proposal_hash"]]
                            if batch["proposal_hash"] is not None
                            else []
                        ),
                        *(
                            await session.scalars(
                                select(artifact_blobs.c.artifact_hash)
                                .select_from(
                                    stage_artifacts.join(
                                        artifact_blobs,
                                        artifact_blobs.c.id
                                        == stage_artifacts.c.artifact_blob_id,
                                    )
                                )
                                .where(
                                    stage_artifacts.c.workflow_run_id
                                    == batch["workflow_run_id"]
                                )
                            )
                        ).all(),
                    }
                ),
                payload={
                    "status": result.status,
                    "relay_actor_id": actor.actor_id,
                    "relay_actor_source": actor.source.value,
                    "idempotency_key_hash": (
                        f"sha256:{sha256(command.idempotency_key.encode()).hexdigest()}"
                    ),
                    "decision_ids": [str(item) for item in decision_ids],
                },
                created_at=timestamp,
            )
        )
        await session.execute(
            insert(outbox).values(
                topic=event_type,
                aggregate_type="review_batch",
                aggregate_id=batch["id"],
                payload={"status": result.status},
                created_at=timestamp,
            )
        )

    async def _idempotent_result(
        self,
        session: AsyncSession,
        *,
        project_id: UUID,
        key: str,
        request_hash: str,
    ) -> ApplyResult | None:
        row = (
            await session.execute(
                select(idempotency_keys).where(
                    idempotency_keys.c.project_id == project_id,
                    idempotency_keys.c.key == key,
                )
            )
        ).mappings().one_or_none()
        if row is None:
            return None
        if row["request_hash"] != request_hash:
            raise IdempotencyConflictError("idempotency key was used with a different body")
        return ApplyResult.model_validate(row["response_body"])

    async def _reserve_command(
        self,
        *,
        batch: Any,
        command: DecisionSubmission,
        request_hash: str,
        actor: Actor,
        confirmed_actor_id: str,
        timestamp: datetime,
    ) -> tuple[str | None, ApplyResult | None]:
        owner_token = uuid4().hex
        async with self.sessions.begin() as session:
            inserted = (
                await session.execute(
                    postgres_insert(review_commands)
                    .values(
                        project_id=batch["project_id"],
                        review_batch_id=batch["id"],
                        key=command.idempotency_key,
                        request_hash=request_hash,
                        confirmed_actor_id=confirmed_actor_id,
                        relay_actor_id=actor.actor_id,
                        confirmation_token_hash=token_hash(command.confirmation_token),
                        status="applying",
                        owner_token=owner_token,
                        lease_expires_at=timestamp + timedelta(seconds=30),
                        attempts=1,
                        updated_at=timestamp,
                    )
                    .on_conflict_do_nothing(
                        index_elements=[review_commands.c.project_id, review_commands.c.key]
                    )
                    .returning(review_commands.c.id)
                )
            ).scalar_one_or_none()
            if inserted is not None:
                return owner_token, None
            row = (
                await session.execute(
                    select(review_commands)
                    .where(
                        review_commands.c.project_id == batch["project_id"],
                        review_commands.c.key == command.idempotency_key,
                    )
                    .with_for_update()
                )
            ).mappings().one()
            if row["request_hash"] != request_hash:
                raise IdempotencyConflictError(
                    "idempotency key was used with a different body"
                )
            if row["response_body"] is not None and row["status"] in {
                "completed",
                "reconciliation_required",
            }:
                return None, ApplyResult.model_validate(row["response_body"])
            if row["lease_expires_at"] is not None and timestamp >= row["lease_expires_at"]:
                await session.execute(
                    update(review_commands)
                    .where(review_commands.c.id == row["id"])
                    .values(
                        owner_token=owner_token,
                        lease_expires_at=timestamp + timedelta(seconds=30),
                        attempts=int(row["attempts"]) + 1,
                        updated_at=timestamp,
                    )
                )
                return owner_token, None
            return None, None

    @staticmethod
    def _assert_token_row(
        row: Any | None,
        nonce: str,
        batch: Any,
        actor_id: str,
        now: datetime,
        *,
        allow_consumed: bool = False,
    ) -> None:
        if row is None:
            raise ConfirmationTokenError("confirmation token was not issued")
        if row["nonce"] != nonce:
            raise ConfirmationTokenError("confirmation token nonce does not match")
        if row["actor_id"] != actor_id:
            raise ConfirmationTokenError("confirmation token actor does not match")
        if row["project_id"] != batch["project_id"]:
            raise ConfirmationTokenError("confirmation token project does not match")
        if row["review_batch_id"] != batch["id"]:
            raise ConfirmationTokenError("confirmation token batch does not match")
        if row["dependency_digest"] != batch["dependency_digest"]:
            raise ConfirmationTokenError("confirmation token digest does not match")
        if row["consumed_at"] is not None and not allow_consumed:
            raise ConfirmationTokenError("confirmation token was already consumed")
        if now >= row["expires_at"]:
            raise ConfirmationTokenError("confirmation token is expired")

    @staticmethod
    async def _batch(
        session: AsyncSession, batch_id: UUID, *, lock: bool = False
    ) -> Any:
        statement = select(review_batches).where(review_batches.c.id == batch_id)
        if lock:
            statement = statement.with_for_update()
        row = (await session.execute(statement)).mappings().one_or_none()
        if row is None:
            raise ReviewNotFoundError(f"review batch not found: {batch_id}")
        return row

    @staticmethod
    async def _quarantine_unexpected_events(
        session: AsyncSession,
        provider_session_id: UUID,
        events: tuple[dict[str, Any], ...],
    ) -> None:
        for event in events:
            canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
            event_hash = f"sha256:{sha256(canonical.encode()).hexdigest()}"
            external_id = str(event.get("id") or event_hash)
            await session.execute(
                postgres_insert(provider_session_events)
                .values(
                    provider_session_id=provider_session_id,
                    external_event_id=external_id,
                    event_hash=event_hash,
                    payload=event,
                    quarantined=True,
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        provider_session_events.c.provider_session_id,
                        provider_session_events.c.external_event_id,
                    ]
                )
            )
        if events:
            await session.execute(
                update(provider_sessions)
                .where(provider_sessions.c.id == provider_session_id)
                .values(status="quarantined", quarantine_reason="unexpected provider events")
            )

    async def _read_after_mutation(
        self,
        session: AsyncSession,
        *,
        provider_session_id: UUID,
        external_document_id: str,
    ) -> Any | None:
        try:
            state = await self.provider.read_document(external_document_id)
        except Exception:
            logger.exception("Post-mutation provider state refresh failed")
            return None
        return state

    @staticmethod
    def _event_id(event: Any) -> str:
        if isinstance(event, str):
            return event
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        return str(event.get("id") or f"sha256:{sha256(canonical.encode()).hexdigest()}")

    @classmethod
    def _event_ids(cls, events: tuple[Any, ...]) -> set[str]:
        return {cls._event_id(event) for event in events}

    @staticmethod
    def _request_hash(batch_id: UUID, command: DecisionSubmission) -> str:
        canonical = json.dumps(
            {"batch_id": str(batch_id), **command.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"sha256:{sha256(canonical.encode()).hexdigest()}"

    @staticmethod
    def _item_dict(item: Any) -> dict[str, Any]:
        return {
            "id": str(item["id"]),
            "title": item["title"],
            "before": item["before_text"],
            "after": item["after_text"],
            "reason": item["reason"],
            "status": item["status"],
            "evidence": item["evidence"] or {},
            "citation": item["citation"],
        }
