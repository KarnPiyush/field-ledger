from __future__ import annotations

import logging
import sys
from typing import Any, cast
from uuid import UUID

from mcp.server.fastmcp import FastMCP

from fieldledger.api.dependencies import (
    ServiceContainer,
    build_provider,
    build_security,
    build_services,
)
from fieldledger.config import get_settings
from fieldledger.domain.models import ReviewDecision
from fieldledger.domain.review import DecisionSubmission
from fieldledger.security import (
    Actor,
    ActorSource,
    AuthorizationError,
    GlobalRole,
    ProjectRole,
    SecurityManager,
)

logger = logging.getLogger(__name__)


def create_mcp_server(
    *,
    services: ServiceContainer,
    security: SecurityManager,
    actor_id: str,
    allowed_project_id: UUID,
) -> FastMCP:
    mcp = FastMCP(
        "FieldLedger",
        instructions="Inspect and propose FieldLedger work; human approval remains in the UI.",
        log_level="ERROR",
    )

    def actor(project_id: str) -> Actor:
        parsed = UUID(project_id)
        if parsed != allowed_project_id:
            raise AuthorizationError("cross-project MCP access is forbidden")
        return Actor(
            actor_id=actor_id,
            source=ActorSource.MCP,
            global_role=GlobalRole.USER,
        )

    @mcp.tool()
    async def create_project(
        name: str,
        description: str | None = None,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await services.projects.ensure_project(
                project_id=allowed_project_id,
                name=name,
                description=description,
                actor=actor(str(allowed_project_id)),
                role=ProjectRole.AGENT,
            ),
        )

    @mcp.tool()
    async def add_fixture_source(
        project_id: str,
        fixture_name: str,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await services.ingestion.add_fixture_source(
                UUID(project_id),
                fixture_name=fixture_name,
                actor=actor(project_id),
            ),
        )

    @mcp.tool()
    async def start_run(
        project_id: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await services.workflow.start_run(
                UUID(project_id),
                actor=actor(project_id),
                idempotency_key=idempotency_key,
            ),
        )

    @mcp.tool()
    async def inspect_run(project_id: str, run_id: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await services.workflow.inspect_run(UUID(run_id), actor(project_id)),
        )

    @mcp.tool()
    async def list_pending_reviews(project_id: str) -> dict[str, Any]:
        items, total = await services.reviews.list_reviews(
            UUID(project_id),
            actor(project_id),
            limit=100,
            offset=0,
            statuses={"open", "review_ready", "preview_ready", "reconciliation_required"},
        )
        return {"items": items, "total": total}

    @mcp.tool()
    async def start_edit(
        project_id: str,
        document_id: str,
        workflow_run_id: str,
        prompt: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await services.providers.start_edit(
                document_id=UUID(document_id),
                workflow_run_id=UUID(workflow_run_id),
                prompt=prompt,
                idempotency_key=idempotency_key,
                actor=actor(project_id),
            ),
        )

    @mcp.tool()
    async def poll_provider_job(
        project_id: str,
        job_id: str,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await services.providers.poll(UUID(job_id), actor(project_id)),
        )

    @mcp.tool()
    async def request_human_confirmation(
        project_id: str,
        batch_id: str,
    ) -> dict[str, str]:
        actor(project_id)
        UUID(batch_id)
        return {
            "status": "ui_action_required",
            "batch_id": batch_id,
            "action": "request_confirmation_in_ui",
        }

    @mcp.tool()
    async def submit_review_decisions(
        project_id: str,
        batch_id: str,
        idempotency_key: str,
        confirmation_token: str,
        decisions: list[dict[str, Any]],
    ) -> dict[str, Any]:
        mcp_actor = actor(project_id)
        command = DecisionSubmission(
            idempotency_key=idempotency_key,
            confirmation_token=confirmation_token,
            decisions=tuple(ReviewDecision.model_validate(item) for item in decisions),
        )
        result = await services.reviews.submit_decisions(
            UUID(batch_id),
            command,
            mcp_actor,
        )
        return cast(dict[str, Any], result.model_dump(mode="json"))

    @mcp.tool()
    async def inspect_audit(project_id: str, limit: int = 50) -> dict[str, Any]:
        bounded_limit = min(max(limit, 1), 100)
        items, total = await services.audit.list_audit(
            UUID(project_id),
            actor(project_id),
            limit=bounded_limit,
            offset=0,
        )
        return {"items": items, "total": total}

    @mcp.tool(name="export")
    async def export_document(
        project_id: str,
        document_id: str,
        format: str,
    ) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            await services.exports.export_document(
                UUID(document_id),
                format=format,
                actor=actor(project_id),
            ),
        )

    return mcp


def build_default_server() -> FastMCP:
    settings = get_settings()
    security = build_security(settings)
    project_value = "00000000-0000-4000-8000-000000000001"
    return create_mcp_server(
        services=build_services(settings, security, build_provider(settings)),
        security=security,
        actor_id="fieldledger-mcp",
        allowed_project_id=UUID(project_value),
    )


def run() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    build_default_server().run(transport="stdio")


if __name__ == "__main__":
    run()
