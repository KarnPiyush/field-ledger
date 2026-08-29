from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from fieldledger.config import Settings
from fieldledger.db import session_factory
from fieldledger.providers.base import DocumentProvider
from fieldledger.providers.deterministic import DeterministicDocumentProvider
from fieldledger.providers.superdocs import SuperDocsProvider
from fieldledger.security import Actor, ProjectAuthorizer, SecurityManager, SessionTokenError
from fieldledger.services.demo_service import DemoSeedService
from fieldledger.services.dependency_service import DependencyService
from fieldledger.services.export_service import ExportService
from fieldledger.services.ingestion_service import IngestionService
from fieldledger.services.project_service import (
    AuditService,
    ProjectService,
    RunService,
    SourceService,
)
from fieldledger.services.provider_service import ProviderService
from fieldledger.services.review_service import ReviewService
from fieldledger.services.workflow_service import WorkflowService


@dataclass(frozen=True)
class ServiceContainer:
    projects: Any
    ingestion: Any
    workflow: Any
    reviews: Any
    audit: Any
    providers: Any
    exports: Any
    demo: Any | None = None


def build_security(settings: Settings) -> SecurityManager:
    return SecurityManager(
        signing_secret=settings.session_signing_secret,
        issuer=settings.session_issuer,
        audience=settings.session_audience,
        local_demo_enabled=settings.local_demo_enabled,
        demo_bootstrap_secret=settings.demo_bootstrap_secret,
        demo_identities=settings.demo_identities,
    )


def build_provider(settings: Settings) -> DocumentProvider:
    if settings.provider == "deterministic":
        return DeterministicDocumentProvider()
    if settings.provider == "superdocs":
        if not settings.superdocs_api_key:
            raise RuntimeError("SUPERDOCS_API_KEY is required only for live SuperDocs mode")
        return SuperDocsProvider(
            api_key=settings.superdocs_api_key,
            timeout_seconds=settings.provider_timeout_seconds,
        )
    raise RuntimeError(f"unsupported provider: {settings.provider}")


def build_services(
    settings: Settings,
    security: SecurityManager,
    provider: DocumentProvider,
) -> ServiceContainer:
    workflow = WorkflowService(session_factory)
    ingestion_core = IngestionService(session_factory, workflow=workflow)
    dependency = DependencyService(
        session_factory,
        policy_content_hash=settings.policy_content_hash,
    )
    authorization = ProjectAuthorizer(session_factory)
    providers = ProviderService(
        session_factory,
        provider,
        authorization=authorization,
        dependencies=dependency,
    )
    return ServiceContainer(
        projects=ProjectService(session_factory, authorization),
        ingestion=SourceService(ingestion_core, authorization),
        workflow=RunService(session_factory, workflow, authorization),
        reviews=ReviewService(
            session_factory,
            provider=provider,
            security=security,
            authorization=authorization,
            dependencies=dependency,
        ),
        audit=AuditService(session_factory, authorization),
        providers=providers,
        exports=ExportService(
            session_factory,
            provider,
            authorization=authorization,
        ),
        demo=DemoSeedService(
            session_factory,
            enabled=settings.local_demo_enabled,
            provider=provider,
            workflow=workflow,
            ingestion=ingestion_core,
            providers=providers,
        ),
    )


def services(request: Request) -> ServiceContainer:
    return cast(ServiceContainer, request.app.state.services)


def security(request: Request) -> SecurityManager:
    return cast(SecurityManager, request.app.state.security)


_bearer = HTTPBearer(auto_error=False)


async def current_actor(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    manager: Annotated[SecurityManager, Depends(security)],
) -> Actor:
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise SessionTokenError("bearer session is required")
    return manager.verify_session(credentials.credentials, now=datetime.now(UTC))


Services = Annotated[ServiceContainer, Depends(services)]
CurrentActor = Annotated[Actor, Depends(current_actor)]
Security = Annotated[SecurityManager, Depends(security)]
