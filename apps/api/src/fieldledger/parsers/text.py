from fieldledger.domain.models import SourceCoordinate, SourceKind
from fieldledger.parsers.base import ParsedChunk, parsed_chunk


class TextParser:
    def __init__(self, kind: SourceKind) -> None:
        self.kind = kind

    def parse(self, content: bytes) -> tuple[ParsedChunk, ...]:
        text = content.decode("utf-8-sig", errors="strict")
        chunks = tuple(
            parsed_chunk(
                ordinal,
                line,
                SourceCoordinate(
                    kind=self.kind,
                    line_start=line_number,
                    line_end=line_number,
                ),
            )
            for ordinal, (line_number, line) in enumerate(
                item for item in enumerate(text.splitlines(), start=1) if item[1].strip()
            )
        )
        if not chunks:
            raise ValueError("text source contains no nonblank lines")
        return chunks
