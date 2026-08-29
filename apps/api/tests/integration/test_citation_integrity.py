from uuid import uuid4

import pytest
from fieldledger.config import Settings
from fieldledger.persistence.tables import (
    citations,
    document_versions,
    documents,
    facts,
    projects,
    source_chunks,
    workflow_runs,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine


@pytest.mark.asyncio
async def test_citation_version_is_derived_from_its_source_chunk() -> None:
    engine = create_async_engine(Settings.from_environment().database_url)
    project_id = uuid4()
    document_id = uuid4()
    version_a_id = uuid4()
    version_b_id = uuid4()
    chunk_b_id = uuid4()
    run_id = uuid4()
    fact_id = uuid4()
    citation_id = uuid4()

    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            await connection.execute(projects.insert().values(id=project_id, name="Integrity test"))
            await connection.execute(
                documents.insert().values(
                    id=document_id,
                    project_id=project_id,
                    name="contract.pdf",
                    media_type="application/pdf",
                    fingerprint="sha256:document",
                )
            )
            await connection.execute(
                document_versions.insert(),
                [
                    {
                        "id": version_a_id,
                        "document_id": document_id,
                        "version_number": 1,
                        "content_hash": "sha256:version-a",
                        "storage_uri": "memory://version-a",
                        "byte_size": 10,
                        "raw_content": b"version-a!",
                    },
                    {
                        "id": version_b_id,
                        "document_id": document_id,
                        "version_number": 2,
                        "content_hash": "sha256:version-b",
                        "storage_uri": "memory://version-b",
                        "byte_size": 11,
                        "raw_content": b"version-b!!",
                    },
                ],
            )
            await connection.execute(
                source_chunks.insert().values(
                    id=chunk_b_id,
                    document_version_id=version_b_id,
                    ordinal=0,
                    content_hash="sha256:chunk-b",
                    content="Version B text",
                    coordinate={"kind": "pdf", "page": 1},
                )
            )
            await connection.execute(
                workflow_runs.insert().values(
                    id=run_id,
                    project_id=project_id,
                    input_hash="sha256:input",
                )
            )
            await connection.execute(
                facts.insert().values(
                    id=fact_id,
                    workflow_run_id=run_id,
                    fact_key="contract_value",
                    value={"amount": 1250000},
                )
            )

            # There is deliberately no independent version field to set to version A.
            await connection.execute(
                citations.insert().values(
                    id=citation_id,
                    fact_id=fact_id,
                    source_chunk_id=chunk_b_id,
                    coordinate={"kind": "pdf", "page": 1},
                    quoted_text="Version B text",
                )
            )

            resolved_version_id = await connection.scalar(
                select(source_chunks.c.document_version_id)
                .select_from(citations.join(source_chunks))
                .where(citations.c.id == citation_id)
            )
            citation_columns = set(
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = current_schema() AND table_name = 'citations'"
                        )
                    )
                ).scalars()
            )

            assert resolved_version_id == version_b_id
            assert resolved_version_id != version_a_id
            assert "document_version_id" not in citation_columns
        finally:
            await transaction.rollback()
    await engine.dispose()
