from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from fieldledger.api.dependencies import CurrentActor, Services
from fieldledger.api.models import AuditListResponse

router = APIRouter(tags=["audit"])


@router.get("/projects/{project_id}/audit", response_model=AuditListResponse)
async def list_audit(
    project_id: UUID,
    actor: CurrentActor,
    container: Services,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, object]:
    items, total = await container.audit.list_audit(
        project_id, actor, limit=limit, offset=offset
    )
    return {"items": items, "total": total, "limit": limit, "offset": offset}
