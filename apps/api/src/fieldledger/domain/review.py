from __future__ import annotations

import json
from datetime import datetime
from hashlib import sha256
from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from fieldledger.domain.models import ContractModel, DependencyKind, ReviewDecision


class DependencyEntry(ContractModel):
    kind: DependencyKind
    key: str = Field(min_length=1)
    content_hash: str = Field(min_length=1)


class DependencyChange(ContractModel):
    kind: DependencyKind
    key: str
    reviewed_hash: str | None
    current_hash: str | None


class DependencyVector(ContractModel):
    entries: tuple[DependencyEntry, ...]

    @model_validator(mode="after")
    def canonicalize(self) -> Self:
        ordered = tuple(
            sorted(self.entries, key=lambda item: (item.kind.value, item.key, item.content_hash))
        )
        identities = [(item.kind.value, item.key) for item in ordered]
        if len(identities) != len(set(identities)):
            raise ValueError("dependency vector contains duplicate kind/key entries")
        object.__setattr__(self, "entries", ordered)
        return self

    @property
    def canonical_json(self) -> str:
        return json.dumps(
            [
                [entry.kind.value, entry.key, entry.content_hash]
                for entry in self.entries
            ],
            ensure_ascii=True,
            separators=(",", ":"),
        )

    @property
    def digest(self) -> str:
        return f"sha256:{sha256(self.canonical_json.encode()).hexdigest()}"

    def diff(self, current: DependencyVector) -> tuple[DependencyChange, ...]:
        reviewed_map = {(item.kind, item.key): item.content_hash for item in self.entries}
        current_map = {(item.kind, item.key): item.content_hash for item in current.entries}
        changes: list[DependencyChange] = []
        for kind, key in sorted(
            set(reviewed_map) | set(current_map), key=lambda item: (item[0].value, item[1])
        ):
            reviewed_hash = reviewed_map.get((kind, key))
            current_hash = current_map.get((kind, key))
            if reviewed_hash != current_hash:
                changes.append(
                    DependencyChange(
                        kind=kind,
                        key=key,
                        reviewed_hash=reviewed_hash,
                        current_hash=current_hash,
                    )
                )
        return tuple(changes)


class DecisionSubmission(ContractModel):
    idempotency_key: str = Field(min_length=1, max_length=255)
    confirmation_token: str = Field(min_length=1)
    decisions: tuple[ReviewDecision, ...]

    @model_validator(mode="after")
    def reject_duplicates(self) -> Self:
        item_ids = [item.item_id for item in self.decisions]
        if len(item_ids) != len(set(item_ids)):
            raise ValueError("duplicate review item decision")
        return self


class ConfirmationGrant(ContractModel):
    token: str
    dependency_digest: str
    expires_at: datetime


class ApplyResult(ContractModel):
    status: Literal[
        "processing",
        "review_ready",
        "preview_ready",
        "completed",
        "failed",
        "cancelled",
        "stale",
        "reconciliation_required",
    ]
    batch_id: UUID
    stale_changes: tuple[DependencyChange, ...] = ()
    next_batch_id: UUID | None = None
    provider_job_id: UUID | None = None
    replayed: bool = False
