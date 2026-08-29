from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from fieldledger.providers.base import ProviderProblem
from fieldledger.security import SecurityError

logger = logging.getLogger(__name__)


class ApiError(RuntimeError):
    def __init__(
        self,
        *,
        status: int,
        code: str,
        title: str,
        detail: str,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.extensions = extensions or {}


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    title: str,
    detail: str,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    correlation_id = getattr(request.state, "correlation_id", "unknown")
    return JSONResponse(
        status_code=status,
        content={
            "type": f"https://fieldledger.local/problems/{code}",
            "title": title,
            "status": status,
            "detail": detail,
            "instance": request.url.path,
            "correlation_id": correlation_id,
            **(extensions or {}),
        },
        headers={"X-Correlation-ID": correlation_id},
        media_type="application/problem+json",
    )


def register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, error: ApiError) -> JSONResponse:
        return problem_response(
            request,
            status=error.status,
            code=error.code,
            title=error.title,
            detail=error.detail,
            extensions=error.extensions,
        )

    @app.exception_handler(SecurityError)
    async def security_error(request: Request, error: SecurityError) -> JSONResponse:
        status = error.status
        title = "Forbidden" if status == 403 else "Authentication failed"
        return problem_response(
            request,
            status=status,
            code=error.code,
            title=title,
            detail=str(error),
        )

    @app.exception_handler(ProviderProblem)
    async def provider_error(request: Request, error: ProviderProblem) -> JSONResponse:
        status = 504 if error.kind == "provider_timeout" else 502
        if error.status == 429:
            status = 429
        return problem_response(
            request,
            status=status,
            code=error.kind,
            title="Provider request failed",
            detail="The provider could not complete the request safely.",
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(
        request: Request, _error: RequestValidationError
    ) -> JSONResponse:
        return problem_response(
            request,
            status=422,
            code="validation-error",
            title="Validation failed",
            detail="The request did not match the API contract.",
        )

    @app.exception_handler(LookupError)
    async def not_found(request: Request, error: LookupError) -> JSONResponse:
        status = int(getattr(error, "status", 404))
        return problem_response(
            request,
            status=status,
            code=str(getattr(error, "code", "not-found")),
            title="Not found",
            detail=str(error),
        )

    @app.exception_handler(RuntimeError)
    async def conflict_or_internal(request: Request, error: RuntimeError) -> JSONResponse:
        if hasattr(error, "status"):
            typed_error = cast(Any, error)
            return problem_response(
                request,
                status=int(typed_error.status),
                code=str(getattr(error, "code", "request-conflict")),
                title="Request conflict",
                detail=str(error),
            )
        logger.exception("Unhandled request error", exc_info=error)
        return problem_response(
            request,
            status=500,
            code="internal-error",
            title="Internal server error",
            detail="The request could not be completed.",
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, error: Exception) -> JSONResponse:
        logger.exception("Unhandled request error", exc_info=error)
        return problem_response(
            request,
            status=500,
            code="internal-error",
            title="Internal server error",
            detail="The request could not be completed.",
        )
