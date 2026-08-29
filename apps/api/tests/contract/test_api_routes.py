from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from fieldledger.api.dependencies import ServiceContainer
from fieldledger.api.errors import ApiError
from fieldledger.domain.review import (
    ApplyResult,
    ConfirmationGrant,
    DecisionSubmission,
    DependencyChange,
)
from fieldledger.main import create_app
from fieldledger.security import (
    ActorSource,
    DemoIdentity,
    GlobalRole,
    SecurityManager,
)
from fieldledger.services.export_service import ExportDocumentNotFoundError
from fieldledger.services.provider_service import ProviderJobNotFoundError

PROJECT_ID = UUID("11111111-1111-4111-8111-111111111111")
BATCH_ID = UUID("22222222-2222-4222-8222-222222222222")
NOW = datetime.now(UTC).replace(microsecond=0)


@dataclass
class RouteServiceFake:
    jobs: dict[UUID, str] = field(default_factory=dict)
    actors: list[str] = field(default_factory=list)
    submitted: list[DecisionSubmission] = field(default_factory=list)
    apply_result: ApplyResult | None = None
    edit_lookup_calls: int = 0
    seed_calls: int = 0

    async def list_projects(self, actor, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        self.actors.append(actor.actor_id)
        return ([{"id": str(PROJECT_ID), "name": "Site Alpha", "status": "active"}], 1)

    async def create_project(self, *, name: str, description: str | None, actor):  # type: ignore[no-untyped-def]
        self.actors.append(actor.actor_id)
        return {"id": str(PROJECT_ID), "name": name, "description": description, "status": "active"}

    async def add_source(self, project_id, *, name: str, media_type: str, content: bytes, actor):  # type: ignore[no-untyped-def]
        return {"project_id": str(project_id), "source_name": name, "status": "parsed"}

    async def start_run(self, project_id, *, actor, idempotency_key: str):  # type: ignore[no-untyped-def]
        return {"id": str(uuid4()), "project_id": str(project_id), "status": "processing"}

    async def inspect_run(self, run_id, actor):  # type: ignore[no-untyped-def]
        return {"id": str(run_id), "status": "review_ready", "current_stage": None}

    async def list_reviews(self, project_id, actor, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        return ([{"id": str(BATCH_ID), "project_id": str(project_id), "status": "open"}], 1)

    async def get_batch(self, batch_id, actor):  # type: ignore[no-untyped-def]
        return {"id": str(batch_id), "project_id": str(PROJECT_ID), "status": "open", "items": []}

    async def request_confirmation(self, batch_id, actor, *, now=None, lifetime=None):  # type: ignore[no-untyped-def]
        self.actors.append(actor.actor_id)
        return ConfirmationGrant(
            token="ui-confirmation-token",
            dependency_digest="sha256:reviewed",
            expires_at=NOW + timedelta(minutes=5),
        )

    async def submit_decisions(self, batch_id, command, actor, *, now=None):  # type: ignore[no-untyped-def]
        self.actors.append(actor.actor_id)
        self.submitted.append(command)
        return self.apply_result or ApplyResult(status="preview_ready", batch_id=batch_id)

    async def list_audit(self, project_id, actor, *, limit: int, offset: int):  # type: ignore[no-untyped-def]
        return ([{"event_type": "review.decisions_applied"}], 1)

    async def get_job(self, job_id, actor):  # type: ignore[no-untyped-def]
        status = self.jobs.get(job_id, "processing")
        return {"id": str(job_id), "status": status}

    async def start_edit(self, *, document_id, workflow_run_id, prompt, idempotency_key, actor):  # type: ignore[no-untyped-def]
        return {"id": uuid4(), "status": "processing", "retry_after_seconds": None}

    async def lookup_edit(self, *, document_id, idempotency_key, actor):  # type: ignore[no-untyped-def]
        self.edit_lookup_calls += 1
        return {
            "id": BATCH_ID,
            "status": "reconciliation_required",
            "retry_after_seconds": None,
        }

    async def seed(self, *, actor, idempotency_key: str):  # type: ignore[no-untyped-def]
        self.seed_calls += 1
        return {
            "project_id": PROJECT_ID,
            "workflow_run_id": UUID("33333333-3333-4333-8333-333333333333"),
            "review_batch_id": BATCH_ID,
            "document_id": UUID("44444444-4444-4444-8444-444444444444"),
            "status": "review_ready",
            "replayed": self.seed_calls > 1,
        }

    async def poll(self, job_id, actor):  # type: ignore[no-untyped-def]
        return {
            "id": job_id,
            "status": "review_ready",
            "review_batch_id": BATCH_ID,
            "retry_after_seconds": None,
            "error": None,
        }

    async def export_document(self, document_id, *, format: str, actor):  # type: ignore[no-untyped-def]
        return {
            "document_id": str(document_id),
            "status": "completed",
            "name": f"export.{format}",
            "media_type": "application/octet-stream",
            "content_base64": "ZmlsZQ==",
        }


def security(*, enabled: bool = True) -> SecurityManager:
    return SecurityManager(
        signing_secret="placeholder-route-secret",
        issuer="fieldledger-route-test",
        audience="fieldledger-route-client",
        local_demo_enabled=enabled,
        demo_bootstrap_secret="placeholder-route-bootstrap",
        demo_identities={
            "reviewer": DemoIdentity(
                actor_id="route-reviewer",
                source=ActorSource.UI,
                global_role=GlobalRole.USER,
            )
        },
    )


def auth_headers(manager: SecurityManager, project_id: UUID = PROJECT_ID) -> dict[str, str]:
    token = manager.mint_demo_session(
        identity="reviewer",
        bootstrap_secret="placeholder-route-bootstrap",
        now=NOW,
        lifetime=timedelta(days=1),
    )
    return {"Authorization": f"Bearer {token}", "X-Correlation-ID": "corr-route-1"}


def client(fake: RouteServiceFake, manager: SecurityManager | None = None) -> TestClient:
    services = ServiceContainer(
        projects=fake,
        ingestion=fake,
        workflow=fake,
        reviews=fake,
        audit=fake,
        providers=fake,
        exports=fake,
    )
    object.__setattr__(services, "demo", fake)
    return TestClient(create_app(services=services, security=manager or security()))


def test_openapi_registers_the_exact_v1_surface() -> None:
    with client(RouteServiceFake()) as api:
        paths = set(api.get("/openapi.json").json()["paths"])

    assert {
        "/api/v1/auth/demo-session",
        "/api/v1/demo/seed",
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/sources",
        "/api/v1/projects/{project_id}/runs",
        "/api/v1/runs/{run_id}",
        "/api/v1/projects/{project_id}/reviews",
        "/api/v1/reviews/{batch_id}",
        "/api/v1/reviews/{batch_id}/confirmation",
        "/api/v1/reviews/{batch_id}/decisions",
        "/api/v1/projects/{project_id}/audit",
        "/api/v1/ops/provider-jobs/{job_id}",
        "/api/v1/ops/provider-jobs/{job_id}/poll",
        "/api/v1/documents/{document_id}/edits",
        "/api/v1/documents/{document_id}/edits/by-idempotency-key",
        "/api/v1/documents/{document_id}/exports",
    } <= paths


def test_demo_seed_is_authenticated_local_demo_only_and_idempotent() -> None:
    manager = security(enabled=True)
    fake = RouteServiceFake()
    headers = {**auth_headers(manager), "Idempotency-Key": "demo-seed-1"}
    with client(fake, manager) as api:
        first = api.post("/api/v1/demo/seed", headers=headers, json={})
        replay = api.post("/api/v1/demo/seed", headers=headers, json={})
        unauthenticated = api.post(
            "/api/v1/demo/seed",
            headers={"Idempotency-Key": "demo-seed-2"},
            json={},
        )

    assert first.status_code == 200
    assert first.json() == {
        "project_id": str(PROJECT_ID),
        "workflow_run_id": "33333333-3333-4333-8333-333333333333",
        "review_batch_id": str(BATCH_ID),
        "document_id": "44444444-4444-4444-8444-444444444444",
        "status": "review_ready",
        "replayed": False,
    }
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert unauthenticated.status_code == 401

    disabled_manager = security(enabled=True)
    disabled_token = disabled_manager.mint_demo_session(
        identity="reviewer",
        bootstrap_secret="placeholder-route-bootstrap",
        now=NOW,
        lifetime=timedelta(days=1),
    )
    disabled_manager.local_demo_enabled = False
    with client(RouteServiceFake(), disabled_manager) as api:
        disabled = api.post(
            "/api/v1/demo/seed",
            headers={
                "Authorization": f"Bearer {disabled_token}",
                "Idempotency-Key": "demo-seed-disabled",
            },
            json={},
        )
    assert disabled.status_code == 403


def test_edit_idempotency_lookup_is_non_mutating_header_contract() -> None:
    manager = security()
    fake = RouteServiceFake()
    document_id = uuid4()
    with client(fake, manager) as api:
        response = api.get(
            f"/api/v1/documents/{document_id}/edits/by-idempotency-key",
            headers={**auth_headers(manager), "Idempotency-Key": "lookup-edit-1"},
        )

    assert response.status_code == 200
    assert response.json()["status"] == "reconciliation_required"
    assert fake.edit_lookup_calls == 1
    schema = client(RouteServiceFake()).get("/openapi.json").json()
    operation = schema["paths"][
        "/api/v1/documents/{document_id}/edits/by-idempotency-key"
    ]["get"]
    assert operation["operationId"] == "lookup_edit_by_idempotency_key"


def test_demo_session_mint_route_is_explicit_and_returns_derived_actor() -> None:
    manager = security(enabled=True)
    with client(RouteServiceFake(), manager) as api:
        response = api.post(
            "/api/v1/auth/demo-session",
            headers={"X-FieldLedger-Demo-Bootstrap": "placeholder-route-bootstrap"},
            json={"identity": "reviewer"},
        )
    assert response.status_code == 201
    body = response.json()
    assert body["access_token"].count(".") == 2
    assert body["actor_id"] == "route-reviewer"
    assert "project_id" not in body
    assert "role" not in body

    with client(RouteServiceFake(), manager) as api:
        self_selected = api.post(
            "/api/v1/auth/demo-session",
            headers={"X-FieldLedger-Demo-Bootstrap": "placeholder-route-bootstrap"},
            json={
                "identity": "reviewer",
                "project_id": str(PROJECT_ID),
                "role": "reviewer",
            },
        )
    assert self_selected.status_code == 422

    with client(RouteServiceFake(), security(enabled=False)) as api:
        disabled = api.post(
            "/api/v1/auth/demo-session",
            headers={"X-FieldLedger-Demo-Bootstrap": "placeholder-route-bootstrap"},
            json={"identity": "reviewer"},
        )
    assert disabled.status_code == 403
    assert disabled.headers["content-type"].startswith("application/problem+json")


def test_auth_validation_pagination_and_domain_errors_are_rfc9457_with_correlation() -> None:
    manager = security()
    fake = RouteServiceFake()
    with client(fake, manager) as api:
        unauthenticated = api.get("/api/v1/projects")
        invalid = api.get(
            "/api/v1/projects?limit=0",
            headers=auth_headers(manager),
        )

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["content-type"].startswith("application/problem+json")
    assert unauthenticated.json()["correlation_id"]
    assert invalid.status_code == 422
    assert invalid.headers["X-Correlation-ID"] == "corr-route-1"
    assert invalid.json()["correlation_id"] == "corr-route-1"
    assert "traceback" not in str(invalid.json()).lower()


def test_confirmation_and_decision_routes_use_signed_actor_not_unsigned_body_fields() -> None:
    manager = security()
    fake = RouteServiceFake()
    headers = auth_headers(manager)
    with client(fake, manager) as api:
        confirmation = api.post(
            f"/api/v1/reviews/{BATCH_ID}/confirmation",
            headers=headers,
            json={},
        )
        invalid_actor_claim = api.post(
            f"/api/v1/reviews/{BATCH_ID}/decisions",
            headers=headers,
            json={
                "idempotency_key": "idem-1",
                "confirmation_token": "ui-confirmation-token",
                "decisions": [],
                "actor_id": "attacker",
            },
        )
        applied = api.post(
            f"/api/v1/reviews/{BATCH_ID}/decisions",
            headers={**headers, "Idempotency-Key": "idem-1"},
            json={
                "confirmation_token": "ui-confirmation-token",
                "decisions": [],
            },
        )

    assert confirmation.status_code == 201
    assert confirmation.json() == {
        "token": "ui-confirmation-token",
        "dependency_digest": "sha256:reviewed",
        "expires_at": (NOW + timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
    }
    assert invalid_actor_claim.status_code == 422
    assert applied.status_code == 200
    assert applied.json()["status"] == "preview_ready"
    assert fake.submitted[0].idempotency_key == "idem-1"
    assert fake.actors and all(item == "route-reviewer" for item in fake.actors)


def test_start_edit_and_poll_are_explicit_post_commands() -> None:
    manager = security()
    fake = RouteServiceFake()
    document_id, run_id = uuid4(), uuid4()
    headers = {**auth_headers(manager), "Idempotency-Key": "edit-1"}
    with client(fake, manager) as api:
        started = api.post(
            f"/api/v1/documents/{document_id}/edits",
            headers=headers,
            json={"workflow_run_id": str(run_id), "prompt": "apply reviewed updates"},
        )
        polled = api.post(
            f"/api/v1/ops/provider-jobs/{started.json()['id']}/poll",
            headers=auth_headers(manager),
            json={},
        )

    assert started.status_code == 202
    assert started.json()["status"] == "processing"
    assert polled.status_code == 200
    assert polled.json()["status"] == "review_ready"
    assert polled.json()["review_batch_id"] == str(BATCH_ID)


def test_stale_review_is_correlated_problem_with_exact_dependency_changes() -> None:
    manager = security()
    fake = RouteServiceFake(
        apply_result=ApplyResult(
            status="stale",
            batch_id=BATCH_ID,
            stale_changes=(
                DependencyChange(
                    kind="source_version",
                    key="source-1",
                    reviewed_hash="sha256:old",
                    current_hash="sha256:new",
                ),
            ),
        )
    )
    with client(fake, manager) as api:
        response = api.post(
            f"/api/v1/reviews/{BATCH_ID}/decisions",
            headers={**auth_headers(manager), "Idempotency-Key": "stale-1"},
            json={"confirmation_token": "token", "decisions": []},
        )

    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["correlation_id"] == "corr-route-1"
    assert response.json()["changes"] == [
        {
            "kind": "source_version",
            "key": "source-1",
            "reviewed_hash": "sha256:old",
            "current_hash": "sha256:new",
        }
    ]


def test_missing_provider_job_and_export_are_domain_404_not_500() -> None:
    manager = security()
    fake = RouteServiceFake()

    async def missing_job(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ProviderJobNotFoundError("provider job not found")

    async def missing_export(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ExportDocumentNotFoundError("document not found")

    fake.get_job = missing_job  # type: ignore[method-assign]
    fake.export_document = missing_export  # type: ignore[method-assign]
    with client(fake, manager) as api:
        job = api.get(
            f"/api/v1/ops/provider-jobs/{uuid4()}", headers=auth_headers(manager)
        )
        exported = api.post(
            f"/api/v1/documents/{uuid4()}/exports",
            headers=auth_headers(manager),
            json={"format": "docx"},
        )

    assert job.status_code == 404
    assert exported.status_code == 404
    assert job.headers["content-type"].startswith("application/problem+json")
    assert exported.headers["content-type"].startswith("application/problem+json")


def test_openapi_has_bearer_security_required_bodies_and_concrete_core_models() -> None:
    with client(RouteServiceFake()) as api:
        schema = api.get("/openapi.json").json()

    schemes = schema["components"]["securitySchemes"]
    assert schemes["HTTPBearer"]["scheme"] == "bearer"
    core_paths = {
        "/api/v1/projects",
        "/api/v1/projects/{project_id}/runs",
        "/api/v1/reviews/{batch_id}",
        "/api/v1/reviews/{batch_id}/decisions",
        "/api/v1/documents/{document_id}/exports",
        "/api/v1/documents/{document_id}/edits",
    }
    for path in core_paths:
        for operation in schema["paths"][path].values():
            if path != "/api/v1/projects" or operation["operationId"].startswith("list_"):
                assert operation["security"] == [{"HTTPBearer": []}]
            success = next(
                response
                for code, response in operation["responses"].items()
                if code.startswith("2")
            )
            assert success["content"]["application/json"]["schema"]
            if operation["operationId"].startswith(("create_", "start_", "submit_", "export_")):
                assert operation["requestBody"]["required"] is True


@pytest.mark.parametrize(
    "status",
    [
        "processing",
        "review_ready",
        "preview_ready",
        "completed",
        "failed",
        "cancelled",
        "stale",
        "reconciliation_required",
    ],
)
def test_provider_job_route_never_collapses_reviewable_or_terminal_status_to_pending(
    status: str,
) -> None:
    manager = security()
    job_id = uuid4()
    fake = RouteServiceFake(jobs={job_id: status})
    with client(fake, manager) as api:
        response = api.get(
            f"/api/v1/ops/provider-jobs/{job_id}",
            headers=auth_headers(manager),
        )

    assert response.status_code == 200
    assert response.json()["status"] == status


def test_internal_error_is_redacted_and_correlated() -> None:
    fake = RouteServiceFake()

    async def broken(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise ApiError(
            status=503,
            code="provider-timeout",
            title="Provider timeout",
            detail="The provider response was not confirmed.",
        )

    fake.get_job = broken  # type: ignore[method-assign]
    manager = security()
    with client(fake, manager) as api:
        response = api.get(
            f"/api/v1/ops/provider-jobs/{uuid4()}",
            headers=auth_headers(manager),
        )
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["correlation_id"] == "corr-route-1"
