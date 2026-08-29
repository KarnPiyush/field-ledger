from __future__ import annotations

import json
from collections.abc import Mapping
from hashlib import sha256

from fieldledger.agent.extraction import CitedFact, ExcludedClaim
from fieldledger.agent.reconciliation import Contradiction
from fieldledger.agent.rules import RuleFinding
from fieldledger.domain.models import ArtifactKind, ContractModel, DependencyKind, StageName


class BlockDependency(ContractModel):
    kind: DependencyKind
    key: str
    content_hash: str


class ArtifactBlock(ContractModel):
    block_key: str
    stage: StageName
    kind: ArtifactKind
    canonical_bytes: bytes
    content_hash: str
    dependencies: tuple[BlockDependency, ...]


def canonical_json(payload: object) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


def build_block(
    *,
    block_key: str,
    stage: StageName,
    kind: ArtifactKind,
    payload: object,
    dependencies: tuple[BlockDependency, ...],
) -> ArtifactBlock:
    content = canonical_json({"block_key": block_key, "payload": payload})
    return ArtifactBlock(
        block_key=block_key,
        stage=stage,
        kind=kind,
        canonical_bytes=content,
        content_hash=f"sha256:{sha256(content).hexdigest()}",
        dependencies=dependencies,
    )


def draft_blocks(
    facts: tuple[CitedFact, ...],
    contradictions: tuple[Contradiction, ...],
    findings: tuple[RuleFinding, ...],
    excluded: tuple[ExcludedClaim, ...],
    upstream: Mapping[str, ArtifactBlock],
) -> tuple[tuple[ArtifactBlock, ...], tuple[ArtifactBlock, ...]]:
    grouped: dict[str, list[CitedFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.fact_key, []).append(fact)
    contradiction_by_key = {item.fact_key: item for item in contradictions}
    findings_by_key: dict[str, list[RuleFinding]] = {}
    for finding in findings:
        findings_by_key.setdefault(finding.fact_key, []).append(finding)
    register: list[ArtifactBlock] = []
    brief: list[ArtifactBlock] = []
    for key, members in sorted(grouped.items()):
        excluded_for_key = tuple(item for item in excluded if item.fact_key == key)
        dependency_blocks = [upstream[f"fact:{member.id}"] for member in members]
        dependency_blocks.extend(
            block
            for block_key, block in upstream.items()
            if block_key.startswith("excluded:") and block_key.endswith(f":{key}")
        )
        contradiction_key = f"contradiction:{key}"
        if contradiction_key in upstream:
            dependency_blocks.append(upstream[contradiction_key])
        for finding in findings_by_key.get(key, []):
            dependency_blocks.append(upstream[f"finding:{finding.rule_id}"])
        dependencies = tuple(
            BlockDependency(
                kind=DependencyKind.PROPOSAL,
                key=f"artifact:{block.block_key}",
                content_hash=block.content_hash,
            )
            for block in sorted(dependency_blocks, key=lambda item: item.block_key)
        )
        payload = {
            "fact_key": key,
            "values": [member.value for member in members],
            "units": sorted({member.unit for member in members if member.unit}),
            "contradiction": contradiction_by_key.get(key) is not None,
            "excluded_claim_count": len(excluded_for_key),
        }
        register.append(
            build_block(
                block_key=f"register:{key}",
                stage=StageName.DRAFT,
                kind=ArtifactKind.CHANGE_REGISTER,
                payload=payload,
                dependencies=dependencies,
            )
        )
        brief.append(
            build_block(
                block_key=f"brief:{key}",
                stage=StageName.DRAFT,
                kind=ArtifactKind.EXECUTIVE_BRIEF,
                payload={
                    "fact_key": key,
                    "summary": payload,
                    "failed_rule_count": sum(
                        item.status == "failed" for item in findings_by_key.get(key, [])
                    ),
                },
                dependencies=dependencies,
            )
        )
    return tuple(register), tuple(brief)
