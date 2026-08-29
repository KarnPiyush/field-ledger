from __future__ import annotations

import base64
import binascii
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query, status
from pydantic import Field

from fieldledger.api.dependencies import CurrentActor, Services
from fieldledger.api.errors import ApiError
from fieldledger.api.models import ProjectListResponse, ProjectResponse, SourceResponse
from fieldledger.domain.models import ContractModel

router = APIRouter(tags=["projects"])


class CreateProjectRequest(ContractModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None


class AddSourceRequest(ContractModel):
    name: str = Field(min_length=1, max_length=255)
    media_type: str = Field(min_length=1, max_length=100)
    content_base64: str = Field(min_length=1)


@router.post(
    "/projects", status_code=status.HTTP_201_CREATED, response_model=ProjectResponse
)
async def create_project(
    body: CreateProjectRequest, actor: CurrentActor, container: Services
) -> object:
    return await container.projects.create_project(
        name=body.name,
        description=body.description,
        actor=actor,
    )


@router.get("/projects", response_model=ProjectListResponse)
async def list_projects(
    actor: CurrentActor,
    container: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    items, total = await container.projects.list_projects(
        actor, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post(
    "/projects/{project_id}/sources",
    status_code=status.HTTP_201_CREATED,
    response_model=SourceResponse,
)
async def add_source(
    project_id: UUID,
    body: AddSourceRequest,
    actor: CurrentActor,
    container: Services,
) -> object:
    try:
        content = base64.b64decode(body.content_base64, validate=True)
    except (ValueError, binascii.Error) as error:
        raise ApiError(
            status=422,
            code="invalid-source-content",
            title="Invalid source content",
            detail="content_base64 must be valid base64.",
        ) from error
    return await container.ingestion.add_source(
        project_id,
        name=body.name,
        media_type=body.media_type,
        content=content,
        actor=actor,
    )
