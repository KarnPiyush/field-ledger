from __future__ import annotations

import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import BinaryIO, Literal, Protocol

from pydantic import Field

from fieldledger.domain.models import ContractModel, ProblemDetails, SourceCoordinate


class SourceTooLargeError(ValueError):
    pass


@dataclass(frozen=True)
class HashedContent:
    content: bytes
    content_hash: str
    size_bytes: int


class ParsedChunk(ContractModel):
    ordinal: int = Field(ge=0)
    content: str
    content_hash: str
    coordinate: SourceCoordinate


class ParseOutcome(ContractModel):
    status: Literal["parsed", "unsupported", "failed"]
    chunks: tuple[ParsedChunk, ...] = ()
    problem: ProblemDetails | None = None


class DocumentParser(Protocol):
    def parse(self, content: bytes) -> tuple[ParsedChunk, ...]: ...


@dataclass(frozen=True)
class ParserRegistration:
    parser: DocumentParser
    media_type: str


def hash_bytes(content: bytes) -> str:
    return f"sha256:{sha256(content).hexdigest()}"


def hash_stream(
    stream: BinaryIO,
    *,
    chunk_size: int = 64 * 1024,
    max_bytes: int | None = None,
) -> HashedContent:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    digest = sha256()
    output = bytearray()
    while chunk := stream.read(chunk_size):
        output.extend(chunk)
        if max_bytes is not None and len(output) > max_bytes:
            raise SourceTooLargeError(f"source exceeds {max_bytes} bytes")
        digest.update(chunk)
    return HashedContent(bytes(output), f"sha256:{digest.hexdigest()}", len(output))


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n")).strip()


def parsed_chunk(ordinal: int, content: str, coordinate: SourceCoordinate) -> ParsedChunk:
    normalized = normalize_text(content)
    return ParsedChunk(
        ordinal=ordinal,
        content=normalized,
        content_hash=hash_bytes(normalized.encode()),
        coordinate=coordinate,
    )


def _problem(status: int, title: str, detail: str) -> ProblemDetails:
    slug = title.lower().replace(" ", "-")
    return ProblemDetails(
        type=f"https://fieldledger.local/problems/{slug}",
        title=title,
        status=status,
        detail=detail,
    )


class ParserRegistry:
    def __init__(self, registrations: Mapping[str, ParserRegistration]) -> None:
        self._registrations = dict(registrations)

    def parse(self, name: str, media_type: str, content: bytes) -> ParseOutcome:
        problem = self.validate_type(name, media_type)
        if problem is not None:
            return ParseOutcome(status="unsupported", problem=problem)
        extension = Path(name).suffix.lower()
        registration = self._registrations[extension]
        try:
            chunks = registration.parser.parse(content)
        except Exception as error:
            return ParseOutcome(
                status="failed",
                problem=_problem(
                    422,
                    "Source parse failed",
                    f"{type(error).__name__}: {error}",
                ),
            )
        return ParseOutcome(status="parsed", chunks=chunks)

    def validate_type(self, name: str, media_type: str) -> ProblemDetails | None:
        extension = Path(name).suffix.lower()
        registration = self._registrations.get(extension)
        if registration is None:
            return _problem(415, "Unsupported source", f"unsupported extension: {extension}")
        if media_type != registration.media_type:
            return _problem(
                415,
                "Source type mismatch",
                f"extension/MIME mismatch for {extension}: {media_type}",
            )
        return None
