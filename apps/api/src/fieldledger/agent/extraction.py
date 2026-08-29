from __future__ import annotations

import re
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from fieldledger.domain.models import ContractModel, FactScalar, SourceCoordinate


class AgentSourceChunk(ContractModel):
    source_name: str
    document_id: UUID
    document_version_id: UUID
    source_chunk_id: UUID
    ordinal: int
    content: str
    content_hash: str
    coordinate: SourceCoordinate


class ResolvedCitation(ContractModel):
    source_name: str
    document_version_id: UUID
    source_chunk_id: UUID
    coordinate: SourceCoordinate
    quoted_text: str

    def resolves(self, chunks: tuple[AgentSourceChunk, ...]) -> bool:
        return any(
            chunk.document_version_id == self.document_version_id
            and chunk.source_chunk_id == self.source_chunk_id
            and chunk.coordinate == self.coordinate
            and self.quoted_text in chunk.content
            for chunk in chunks
        )


class CitedFact(ContractModel):
    id: UUID
    fact_key: str
    value: FactScalar
    unit: str | None
    citation: ResolvedCitation


class ExcludedClaim(ContractModel):
    fact_key: str
    value: str
    reason: str
    citation: ResolvedCitation


_DOCUMENT_TYPES = {
    "contract": "contract",
    "change order": "change_order",
    "site report": "site_report",
    "invoice": "invoice",
}
_MONEY_PATTERNS = {
    "contract_value": re.compile(r"^Contract value:\s*USD\s+([0-9]+(?:\.[0-9]+)?)$"),
    "approved_change_value": re.compile(
        r"^Approved change value:\s*USD\s+([0-9]+(?:\.[0-9]+)?)$"
    ),
    "invoice_amount": re.compile(r"^Invoice amount:\s*USD\s+([0-9]+(?:\.[0-9]+)?)$"),
}
_DATE_PATTERN = re.compile(r"^Revised completion date:\s*(\d{4}-\d{2}-\d{2})$")
_QUANTITY_PATTERN = re.compile(r"^Installed quantity:\s*([0-9]+(?:\.[0-9]+)?)\s+(.+)$")
_UNSUPPORTED_PATTERN = re.compile(r"^Unsupported claim:\s*([a-zA-Z0-9_]+)\s*=\s*(.+)$")


def _number(value: str) -> int | float:
    parsed = float(value)
    return int(parsed) if parsed.is_integer() else parsed


def _citation(chunk: AgentSourceChunk, quote: str) -> ResolvedCitation:
    return ResolvedCitation(
        source_name=chunk.source_name,
        document_version_id=chunk.document_version_id,
        source_chunk_id=chunk.source_chunk_id,
        coordinate=chunk.coordinate,
        quoted_text=quote,
    )


def classify_sources(chunks: tuple[AgentSourceChunk, ...]) -> dict[str, str]:
    grouped: dict[str, list[AgentSourceChunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.source_name, []).append(chunk)
    classifications: dict[str, str] = {}
    for source_name, source_chunks in sorted(grouped.items()):
        classification = "unknown"
        for chunk in source_chunks:
            for line in chunk.content.splitlines():
                prefix = "Document type:"
                if line.strip().lower().startswith(prefix.lower()):
                    candidate = line.strip()[len(prefix) :].strip().lower()
                    classification = _DOCUMENT_TYPES.get(candidate, "unsupported")
                    break
            if classification != "unknown":
                break
        classifications[source_name] = classification
    return classifications


def extract_chunks(
    chunks: tuple[AgentSourceChunk, ...],
) -> tuple[tuple[CitedFact, ...], tuple[ExcludedClaim, ...]]:
    facts: list[CitedFact] = []
    excluded: list[ExcludedClaim] = []
    for chunk in chunks:
        for raw_line in chunk.content.splitlines():
            line = raw_line.strip()
            fact: tuple[str, FactScalar, str | None] | None = None
            for key, pattern in _MONEY_PATTERNS.items():
                if match := pattern.fullmatch(line):
                    fact = (key, _number(match.group(1)), "USD")
                    break
            if fact is None and (match := _DATE_PATTERN.fullmatch(line)):
                fact = ("revised_completion_date", match.group(1), None)
            if fact is None and (match := _QUANTITY_PATTERN.fullmatch(line)):
                fact = ("installed_quantity", _number(match.group(1)), match.group(2).strip())
            if fact is not None:
                fact_key, value, unit = fact
                identity = f"{fact_key}|{value}|{chunk.source_chunk_id}|{line}"
                facts.append(
                    CitedFact(
                        id=uuid5(NAMESPACE_URL, identity),
                        fact_key=fact_key,
                        value=value,
                        unit=unit,
                        citation=_citation(chunk, line),
                    )
                )
                continue
            if match := _UNSUPPORTED_PATTERN.fullmatch(line):
                excluded.append(
                    ExcludedClaim(
                        fact_key=match.group(1),
                        value=match.group(2),
                        reason="unsupported",
                        citation=_citation(chunk, line),
                    )
                )
    return tuple(facts), tuple(excluded)


def normalized_chunk_hash(content: str) -> str:
    return f"sha256:{sha256(content.encode()).hexdigest()}"


def fact_payload(fact: CitedFact) -> dict[str, Any]:
    return fact.model_dump(mode="json", exclude_none=True)
