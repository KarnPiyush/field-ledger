from __future__ import annotations

from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import Field, model_validator

from fieldledger.agent.extraction import CitedFact
from fieldledger.domain.models import ContractModel, FactScalar

RuleOperator = Literal["equals", "lte", "gte", "date_on_or_before"]


class RuleDefinition(ContractModel):
    id: str
    fact_key: str
    operator: RuleOperator
    expected: FactScalar

    @model_validator(mode="after")
    def validate_operator_value(self) -> RuleDefinition:
        if self.operator in {"lte", "gte"} and (
            isinstance(self.expected, bool) or not isinstance(self.expected, (int, float))
        ):
            raise ValueError(f"{self.operator} expected must be numeric")
        if self.operator == "date_on_or_before":
            if not isinstance(self.expected, str):
                raise ValueError("date_on_or_before expected must be an ISO date string")
            try:
                date.fromisoformat(self.expected)
            except ValueError as error:
                raise ValueError(
                    "date_on_or_before expected must be an ISO date string"
                ) from error
        return self


class RuleSet(ContractModel):
    version: str
    rules: tuple[RuleDefinition, ...] = Field(default=())


class RuleFinding(ContractModel):
    rule_id: str
    rule_version: str
    fact_key: str
    operator: RuleOperator
    expected: FactScalar
    observed: tuple[FactScalar, ...]
    fact_ids: tuple[UUID, ...]
    status: Literal["passed", "failed", "not_applicable"]


def _apply(operator: RuleOperator, observed: FactScalar, expected: FactScalar) -> bool:
    if operator == "equals":
        return observed == expected
    if operator == "lte":
        return (
            not isinstance(observed, bool)
            and isinstance(observed, (int, float))
            and isinstance(expected, (int, float))
            and float(observed) <= float(expected)
        )
    if operator == "gte":
        return (
            not isinstance(observed, bool)
            and isinstance(observed, (int, float))
            and isinstance(expected, (int, float))
            and float(observed) >= float(expected)
        )
    if not isinstance(observed, str) or not isinstance(expected, str):
        return False
    try:
        return date.fromisoformat(observed) <= date.fromisoformat(expected)
    except ValueError:
        return False


def evaluate_rules(
    facts: tuple[CitedFact, ...], raw_rules: dict[str, Any]
) -> tuple[RuleFinding, ...]:
    rule_set = RuleSet.model_validate(raw_rules)
    grouped: dict[str, list[CitedFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.fact_key, []).append(fact)
    findings: list[RuleFinding] = []
    for rule in rule_set.rules:
        members = grouped.get(rule.fact_key, [])
        status: Literal["passed", "failed", "not_applicable"] = "not_applicable"
        if members:
            status = (
                "passed"
                if all(_apply(rule.operator, member.value, rule.expected) for member in members)
                else "failed"
            )
        findings.append(
            RuleFinding(
                rule_id=rule.id,
                rule_version=rule_set.version,
                fact_key=rule.fact_key,
                operator=rule.operator,
                expected=rule.expected,
                observed=tuple(member.value for member in members),
                fact_ids=tuple(member.id for member in members),
                status=status,
            )
        )
    return tuple(findings)
