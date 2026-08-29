from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


def project_mutation_key(project_id: UUID) -> str:
    return f"project-mutation:{project_id}"


async def acquire_project_mutation_fence(
    session: AsyncSession, project_id: UUID
) -> None:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": project_mutation_key(project_id)},
    )


@asynccontextmanager
async def project_mutation_fence(
    session: AsyncSession, project_id: UUID
) -> AsyncIterator[None]:
    await acquire_project_mutation_fence(session, project_id)
    yield
