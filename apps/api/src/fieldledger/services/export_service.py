from __future__ import annotations

import base64
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.persistence.tables import documents, provider_sessions
from fieldledger.providers.base import DocumentProvider, ExportRequest
from fieldledger.security import Actor, ProjectAuthorizer


class ExportDocumentNotFoundError(LookupError):
    status = 404
    code = "export-document-not-found"


class ExportService:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        provider: DocumentProvider,
        *,
        authorization: ProjectAuthorizer,
    ) -> None:
        self.sessions = sessions
        self.provider = provider
        self.authorization = authorization

    async def export_document(
        self,
        document_id: UUID,
        *,
        format: str,
        actor: Actor,
    ) -> dict[str, Any]:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(
                        documents.c.project_id,
                        provider_sessions.c.external_document_id,
                    )
                    .join(
                        provider_sessions,
                        provider_sessions.c.document_id == documents.c.id,
                    )
                    .where(documents.c.id == document_id)
                )
            ).one_or_none()
            if row is None:
                raise ExportDocumentNotFoundError(f"document not found: {document_id}")
            await self.authorization.require(actor, row.project_id, session=session)
        exported = await self.provider.export(
            ExportRequest(document_id=row.external_document_id, format=format)
        )
        return {
            "document_id": str(document_id),
            "status": "completed",
            "name": exported.name,
            "media_type": exported.media_type,
            "content_base64": base64.b64encode(exported.content).decode(),
        }
