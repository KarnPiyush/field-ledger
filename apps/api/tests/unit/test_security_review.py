from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fieldledger.domain.models import DependencyKind, ReviewDecision, ReviewDecisionKind
from fieldledger.domain.review import (
    DecisionSubmission,
    DependencyEntry,
    DependencyVector,
)
from fieldledger.security import (
    ActorSource,
    AuthorizationError,
    DemoIdentity,
    GlobalRole,
    SecurityManager,
    SessionTokenError,
)
from pydantic import ValidationError

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)
PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
BATCH_ID = UUID("22222222-2222-4222-8222-222222222222")


def security(*, local_demo_enabled: bool = True) -> SecurityManager:
    return SecurityManager(
        signing_secret="placeholder-local-test-secret",
        issuer="fieldledger-test",
        audience="fieldledger-test-client",
        local_demo_enabled=local_demo_enabled,
        demo_bootstrap_secret="placeholder-demo-bootstrap",
        demo_identities={
            "reviewer": DemoIdentity(
                actor_id="demo-reviewer",
                source=ActorSource.UI,
                global_role=GlobalRole.USER,
            )
        },
    )


def test_demo_session_mints_only_a_server_configured_identity() -> None:
    token = security().mint_demo_session(
        identity="reviewer",
        bootstrap_secret="placeholder-demo-bootstrap",
        now=NOW,
        lifetime=timedelta(minutes=10),
    )
    actor = security().verify_session(token, now=NOW + timedelta(minutes=1))

    assert actor.actor_id == "demo-reviewer"
    assert actor.source is ActorSource.UI
    assert actor.global_role is GlobalRole.USER
    assert not hasattr(actor, "project_id")
    assert not hasattr(actor, "role")


@pytest.mark.parametrize(
    ("identity", "bootstrap_secret"),
    [
        ("reviewer", "wrong-placeholder-bootstrap"),
        ("self-selected-admin", "placeholder-demo-bootstrap"),
    ],
)
def test_demo_session_refuses_unknown_identity_or_wrong_bootstrap(
    identity: str, bootstrap_secret: str
) -> None:
    with pytest.raises(AuthorizationError):
        security().mint_demo_session(
            identity=identity,
            bootstrap_secret=bootstrap_secret,
            now=NOW,
        )


def test_demo_session_minting_is_disabled_unless_explicitly_enabled() -> None:
    with pytest.raises(AuthorizationError, match="local demo"):
        security(local_demo_enabled=False).mint_demo_session(
            identity="reviewer",
            bootstrap_secret="placeholder-demo-bootstrap",
            now=NOW,
        )


def test_session_rejects_tampering_wrong_audience_and_expiry() -> None:
    manager = security()
    token = manager.mint_demo_session(
        identity="reviewer",
        bootstrap_secret="placeholder-demo-bootstrap",
        now=NOW,
        lifetime=timedelta(seconds=30),
    )
    head, body, signature = token.split(".")

    with pytest.raises(SessionTokenError):
        manager.verify_session(f"{head}.{body}.{signature[:-1]}x", now=NOW)
    with pytest.raises(SessionTokenError, match="expired"):
        manager.verify_session(token, now=NOW + timedelta(seconds=31))
    with pytest.raises(SessionTokenError, match="audience"):
        SecurityManager(
            signing_secret="placeholder-local-test-secret",
            issuer="fieldledger-test",
            audience="another-client",
            local_demo_enabled=True,
            demo_bootstrap_secret="placeholder-demo-bootstrap",
            demo_identities={
                "reviewer": DemoIdentity(
                    actor_id="demo-reviewer",
                    source=ActorSource.UI,
                    global_role=GlobalRole.USER,
                )
            },
        ).verify_session(token, now=NOW)


def test_actor_cannot_carry_a_self_selected_project_role() -> None:
    actor = security().actor(
        actor_id="human-1",
        source=ActorSource.UI,
        global_role=GlobalRole.USER,
    )

    assert actor.model_dump(mode="json") == {
        "actor_id": "human-1",
        "source": "ui",
        "global_role": "user",
    }


def test_dependency_vector_has_one_canonical_sort_and_sha256_digest() -> None:
    vector = DependencyVector(
        entries=(
            DependencyEntry(
                kind=DependencyKind.SOURCE_VERSION,
                key="source-b",
                content_hash="sha256:bbb",
            ),
            DependencyEntry(
                kind=DependencyKind.TARGET_DOCUMENT_VERSION,
                key="target-a",
                content_hash="sha256:aaa",
            ),
            DependencyEntry(
                kind=DependencyKind.POLICY_VERSION,
                key="fieldledger-policy",
                content_hash="sha256:ccc",
            ),
        )
    )

    assert [(entry.kind.value, entry.key) for entry in vector.entries] == [
        ("policy_version", "fieldledger-policy"),
        ("source_version", "source-b"),
        ("target_document_version", "target-a"),
    ]
    assert vector.canonical_json == (
        '[["policy_version","fieldledger-policy","sha256:ccc"],'
        '["source_version","source-b","sha256:bbb"],'
        '["target_document_version","target-a","sha256:aaa"]]'
    )
    assert vector.digest == (
        "sha256:775694c92eda3a62c170a505b7906c70ba2eb43308002ed880e3476ae833f44c"
    )


def test_dependency_diff_reports_exact_old_and_new_entries() -> None:
    reviewed = DependencyVector(
        entries=(
            DependencyEntry(
                kind=DependencyKind.SOURCE_VERSION,
                key="source",
                content_hash="sha256:old",
            ),
        )
    )
    current = DependencyVector(
        entries=(
            DependencyEntry(
                kind=DependencyKind.SOURCE_VERSION,
                key="source",
                content_hash="sha256:new",
            ),
        )
    )

    assert [change.model_dump(mode="json") for change in reviewed.diff(current)] == [
        {
            "kind": "source_version",
            "key": "source",
            "reviewed_hash": "sha256:old",
            "current_hash": "sha256:new",
        }
    ]


def test_decision_submission_rejects_duplicate_items_and_blank_rejection_notes() -> None:
    item_id = uuid4()
    approve = ReviewDecision(item_id=item_id, decision=ReviewDecisionKind.APPROVE)

    with pytest.raises(ValidationError, match="duplicate"):
        DecisionSubmission(
            idempotency_key="idem",
            confirmation_token="token",
            decisions=(approve, approve),
        )
    with pytest.raises(ValidationError, match="note"):
        ReviewDecision(
            item_id=item_id,
            decision=ReviewDecisionKind.REJECT,
            note="   ",
        )
