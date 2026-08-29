from fieldledger.domain.models import SourceKind
from fieldledger.parsers.base import ParserRegistration, ParserRegistry
from fieldledger.parsers.docx import DocxParser
from fieldledger.parsers.pdf import PdfParser
from fieldledger.parsers.text import TextParser


def default_registry() -> ParserRegistry:
    return ParserRegistry(
        {
            ".pdf": ParserRegistration(PdfParser(), "application/pdf"),
            ".docx": ParserRegistration(
                DocxParser(),
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            ".txt": ParserRegistration(TextParser(SourceKind.TEXT), "text/plain"),
            ".md": ParserRegistration(TextParser(SourceKind.MARKDOWN), "text/markdown"),
        }
    )


__all__ = ["default_registry"]
