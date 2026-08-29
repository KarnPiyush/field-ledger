from __future__ import annotations

import json
from hashlib import sha256
from io import BytesIO
from pathlib import Path

from fieldledger.domain.models import SourceCoordinate
from fieldledger.parsers import default_registry
from fieldledger.parsers.base import hash_stream
from fixtures.generate import fixture_bytes

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"


def test_stream_hash_reads_raw_bytes_in_bounded_chunks() -> None:
    content = b"a" * 25
    hashed = hash_stream(BytesIO(content), chunk_size=7, max_bytes=30)

    assert hashed.content == content
    assert hashed.content_hash == f"sha256:{sha256(content).hexdigest()}"
    assert hashed.size_bytes == 25


def test_all_supported_parsers_resolve_oracle_quotes_at_exact_coordinates() -> None:
    oracle = json.loads((FIXTURE_DIR / "golden_oracle.json").read_text())
    sources = fixture_bytes()
    media_types = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".txt": "text/plain",
        ".md": "text/markdown",
    }
    registry = default_registry()

    for expected in oracle["facts"]:
        name = expected["source"]
        outcome = registry.parse(name, media_types[Path(name).suffix], sources[name])
        assert outcome.status == "parsed"
        coordinate = SourceCoordinate.model_validate(expected["coordinate"])
        matching = [chunk for chunk in outcome.chunks if chunk.coordinate == coordinate]
        assert len(matching) == 1
        assert expected["quoted_text"] in matching[0].content
        assert matching[0].content_hash == (
            f"sha256:{sha256(matching[0].content.encode()).hexdigest()}"
        )


def test_registry_reports_unsupported_and_corrupt_files_without_raising() -> None:
    registry = default_registry()

    legacy = registry.parse("legacy.doc", "application/msword", b"legacy")
    corrupt = registry.parse("broken.pdf", "application/pdf", b"not a pdf")
    mismatch = registry.parse("invoice.md", "application/pdf", b"text")

    assert legacy.status == "unsupported"
    assert legacy.problem is not None and legacy.problem.status == 415
    assert corrupt.status == "failed"
    assert corrupt.problem is not None and corrupt.problem.status == 422
    assert mismatch.status == "unsupported"
    assert mismatch.problem is not None and "mismatch" in mismatch.problem.detail
