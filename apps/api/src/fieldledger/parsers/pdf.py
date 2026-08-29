from io import BytesIO

from pypdf import PdfReader

from fieldledger.domain.models import SourceCoordinate, SourceKind
from fieldledger.parsers.base import ParsedChunk, parsed_chunk


class PdfParser:
    def parse(self, content: bytes) -> tuple[ParsedChunk, ...]:
        reader = PdfReader(BytesIO(content), strict=True)
        chunks: list[ParsedChunk] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            if not text.strip():
                continue
            chunks.append(
                parsed_chunk(
                    len(chunks),
                    text,
                    SourceCoordinate(kind=SourceKind.PDF, page=page_number),
                )
            )
        if not chunks:
            raise ValueError("PDF contains no extractable text")
        return tuple(chunks)
