from __future__ import annotations

import pytest
from fieldledger.agent.rules import RuleSet
from pydantic import ValidationError


@pytest.mark.parametrize(
    ("operator", "expected"),
    (
        ("lte", "100"),
        ("gte", True),
        ("date_on_or_before", 20270101),
        ("date_on_or_before", "not-a-date"),
    ),
)
def test_operator_specific_expected_values_are_validated(
    operator: str, expected: object
) -> None:
    with pytest.raises(ValidationError):
        RuleSet.model_validate(
            {
                "version": "rules-v1",
                "rules": [
                    {
                        "id": "typed-rule",
                        "fact_key": "invoice_amount",
                        "operator": operator,
                        "expected": expected,
                    }
                ],
            }
        )


def test_numeric_and_iso_date_rule_values_are_accepted() -> None:
    rules = RuleSet.model_validate(
        {
            "version": "rules-v1",
            "rules": [
                {"id": "n", "fact_key": "quantity", "operator": "gte", "expected": 1.5},
                {
                    "id": "d",
                    "fact_key": "date",
                    "operator": "date_on_or_before",
                    "expected": "2027-01-01",
                },
            ],
        }
    )
    assert len(rules.rules) == 2
