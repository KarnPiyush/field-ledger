from uuid import UUID

from fastapi import APIRouter, status
from pydantic import Field

from fieldledger.api.dependencies import CurrentActor, Services
from fieldledger.api.models import ExportResponse
from fieldledger.domain.models import ContractModel

router = APIRouter(tags=["exports"])


class ExportRequestBody(ContractModel):
    format: str = Field(min_length=1, max_length=16)


@router.post(
    "/documents/{document_id}/exports",
    status_code=status.HTTP_201_CREATED,
    response_model=ExportResponse,
)
async def export_document(
    document_id: UUID,
    body: ExportRequestBody,
    actor: CurrentActor,
    container: Services,
) -> object:
    return await container.exports.export_document(
        document_id,
        format=body.format,
        actor=actor,
    )
