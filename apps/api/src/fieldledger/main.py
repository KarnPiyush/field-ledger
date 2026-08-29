from collections.abc import Awaitable, Callable
from typing import Annotated
from uuid import uuid4

import uvicorn
from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.responses import Response

from fieldledger.api.dependencies import (
    ServiceContainer,
    build_provider,
    build_security,
    build_services,
)
from fieldledger.api.errors import register_error_handlers
from fieldledger.api.routes import (
    audit,
    auth,
    demo,
    documents,
    exports,
    ops,
    projects,
    reviews,
    runs,
)
from fieldledger.config import get_settings
from fieldledger.db import check_database
from fieldledger.domain.models import ProblemDetails
from fieldledger.security import SecurityManager


async def live() -> dict[str, str]:
    return {"status": "live"}


async def ready(database_ready: Annotated[bool, Depends(check_database)]) -> JSONResponse:
    if database_ready:
        return JSONResponse({"status": "ready", "database": "ready"})

    problem = ProblemDetails(
        type="https://fieldledger.local/problems/database-unavailable",
        title="Database unavailable",
        status=503,
        detail="The readiness probe could not reach PostgreSQL.",
    )
    return JSONResponse(
        status_code=problem.status,
        content=problem.model_dump(mode="json", exclude_none=True),
        media_type="application/problem+json",
    )


def create_app(
    *,
    services: ServiceContainer | None = None,
    security: SecurityManager | None = None,
) -> FastAPI:
    settings = get_settings()
    security_manager = security or build_security(settings)
    service_container = services or build_services(
        settings,
        security_manager,
        build_provider(settings),
    )
    application = FastAPI(title="FieldLedger API", version="0.1.0")
    application.state.services = service_container
    application.state.security = security_manager

    @application.middleware("http")
    async def correlation(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied = request.headers.get("X-Correlation-ID", "").strip()
        request.state.correlation_id = supplied[:128] or str(uuid4())
        response = await call_next(request)
        response.headers["X-Correlation-ID"] = request.state.correlation_id
        return response

    register_error_handlers(application)
    application.add_api_route("/health/live", live, methods=["GET"])
    application.add_api_route("/health/ready", ready, methods=["GET"])
    for router in (
        auth.router,
        demo.router,
        projects.router,
        runs.router,
        reviews.router,
        audit.router,
        documents.router,
        ops.router,
        exports.router,
    ):
        application.include_router(router, prefix="/api/v1")
    return application


app = create_app()


def run() -> None:
    settings = get_settings()
    uvicorn.run(
        "fieldledger.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
    )


if __name__ == "__main__":
    run()
