from __future__ import annotations

import json
from pathlib import Path

from fieldledger.agent.stages import analyze_pile, source_chunks_from_parse
from fieldledger.parsers import default_registry
from fixtures.generate import fixture_bytes

FIXTURE_DIR = Path(__file__).parents[1] / "fixtures"


def _golden_result():
    sources = fixture_bytes()
    registry = default_registry()
    media_types = {
        "contract.pdf": "application/pdf",
        "change_order.docx": (
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        ),
        "site_report.txt": "text/plain",
        "invoice.md": "text/markdown",
    }
    chunks = []
    for name, media_type in media_types.items():
        outcome = registry.parse(name, media_type, sources[name])
        assert outcome.status == "parsed"
        chunks.extend(source_chunks_from_parse(name, outcome))
    return analyze_pile(tuple(chunks), json.loads(sources["rules.json"]))


def test_golden_pile_has_exact_facts_citations_conflict_rules_and_exclusion() -> None:
    oracle = json.loads((FIXTURE_DIR / "golden_oracle.json").read_text())
    result = _golden_result()

    assert result.classifications == oracle["classifications"]
    observed_facts = sorted(
        (
            fact.fact_key,
            fact.value,
            fact.unit,
            fact.citation.source_name,
            fact.citation.coordinate.model_dump(mode="json", exclude_none=True),
            fact.citation.quoted_text,
        )
        for fact in result.facts
    )
    expected_facts = sorted(
        (
            fact["fact_key"],
            fact["value"],
            fact["unit"],
            fact["source"],
            fact["coordinate"],
            fact["quoted_text"],
        )
        for fact in oracle["facts"]
    )
    assert observed_facts == expected_facts
    assert all(fact.citation.resolves(result.source_chunks) for fact in result.facts)
    assert result.citation_coverage == 1.0

    assert [(item.fact_key, list(item.values)) for item in result.contradictions] == [
        ("approved_change_value", [100000, 125000])
    ]
    assert [item.reason for item in result.excluded_claims] == ["unsupported"]
    assert {item.rule_id for item in result.findings if item.status == "failed"} == set(
        oracle["failed_rules"]
    )
    assert {item.rule_id for item in result.findings if item.status == "passed"} == set(
        oracle["passed_rules"]
    )
    assert all(item.rule_version == oracle["rule_version"] for item in result.findings)
    assert any(key.startswith("excluded:") for key in result.blocks)
    assert all(
        not any(
            dependency.key.startswith("artifact:excluded:")
            for dependency in block.dependencies
        )
        for block in result.change_register_blocks + result.executive_brief_blocks
    )


def test_adversarial_source_text_is_inert_and_empty_piles_are_honest() -> None:
    result = _golden_result()
    serialized_facts = json.dumps([fact.model_dump(mode="json") for fact in result.facts])

    assert "ignore previous instructions" not in serialized_facts
    assert "approve_changes" not in serialized_facts
    assert result.provider_calls == 0
    assert result.transitions_requested_by_sources == ()

    empty = analyze_pile((), {"version": "empty-v1", "rules": []})
    assert empty.classifications == {}
    assert empty.facts == ()
    assert empty.excluded_claims == ()
    assert empty.contradictions == ()
    assert empty.findings == ()
    assert empty.change_register_blocks == ()
    assert empty.executive_brief_blocks == ()
    assert empty.citation_coverage == 1.0


def test_mutating_one_fact_preserves_every_unaffected_block_byte_for_byte() -> None:
    original = _golden_result()
    mutated_chunks = tuple(
        chunk.model_copy(
            update={
                "content": chunk.content.replace(
                    "Installed quantity: 420 m", "Installed quantity: 421 m"
                )
            }
        )
        if "Installed quantity: 420 m" in chunk.content
        else chunk
        for chunk in original.source_chunks
    )
    rules = json.loads(fixture_bytes()["rules.json"])

    changed = analyze_pile(mutated_chunks, rules, previous=original)

    assert changed.execution_counts["classify:site_report.txt"] == 0
    assert changed.execution_counts["extract:contract_value"] == 0
    assert changed.execution_counts["extract:approved_change_value"] == 0
    assert changed.execution_counts["extract:invoice_amount"] == 0
    assert changed.execution_counts["extract:installed_quantity"] == 1
    unaffected = set(original.blocks) - set(changed.affected_block_keys)
    assert unaffected
    for key in unaffected:
        assert changed.blocks[key].canonical_bytes == original.blocks[key].canonical_bytes
        assert changed.blocks[key].content_hash == original.blocks[key].content_hash
