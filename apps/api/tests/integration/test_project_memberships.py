from __future__ import annotations

from uuid import uuid4

import pytest
from fieldledger.config import Settings
from fieldledger.persistence.tables import project_memberships, projects
from fieldledger.security import Actor, ActorSource, GlobalRole, ProjectAuthorizer, ProjectRole
from fieldledger.services.project_service import ProjectService
from sqlalchemy import delete, insert, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

pytestmark = pytest.mark.asyncio


async def test_project_creation_and_listing_are_membership_backed() -> None:
    engine = create_async_engine(Settings.from_environment().database_url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = Actor(
        actor_id="membership-owner",
        source=ActorSource.UI,
        global_role=GlobalRole.USER,
    )
    authorization = ProjectAuthorizer(sessions)
    hidden_project_id = uuid4()
    created_id = None
    async with engine.begin() as connection:
        await connection.execute(
            insert(projects).values(id=hidden_project_id, name="Hidden project")
        )

    try:
        service = ProjectService(sessions, authorization)
        created = await service.create_project(
            name="Membership project", description=None, actor=actor
        )
        created_id = created["id"]
        assert created_id != hidden_project_id
        async with sessions() as session:
            membership = (
                await session.execute(
                    select(project_memberships).where(
                        project_memberships.c.project_id == created_id,
                        project_memberships.c.actor_id == actor.actor_id,
                    )
                )
            ).mappings().one()
        assert membership["role"] == ProjectRole.REVIEWER.value

        listed, total = await service.list_projects(actor, limit=50, offset=0)
        assert total == 1
        assert [item["id"] for item in listed] == [created_id]
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                delete(projects).where(
                    projects.c.id.in_(
                        [value for value in (hidden_project_id, created_id) if value is not None]
                    )
                )
            )
        await engine.dispose()
