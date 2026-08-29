from __future__ import annotations

import json
from uuid import UUID

from fieldledger.agent.extraction import CitedFact
from fieldledger.domain.models import ContractModel, FactScalar


class Contradiction(ContractModel):
    fact_key: str
    values: tuple[FactScalar, ...]
    fact_ids: tuple[UUID, ...]


def _sort_key(value: FactScalar) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def reconcile_facts(facts: tuple[CitedFact, ...]) -> tuple[Contradiction, ...]:
    grouped: dict[str, list[CitedFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.fact_key, []).append(fact)
    contradictions: list[Contradiction] = []
    for key, members in sorted(grouped.items()):
        values = tuple(sorted({member.value for member in members}, key=_sort_key))
        if len(values) > 1:
            contradictions.append(
                Contradiction(
                    fact_key=key,
                    values=values,
                    fact_ids=tuple(sorted((member.id for member in members), key=str)),
                )
            )
    return tuple(contradictions)
