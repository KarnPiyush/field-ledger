from __future__ import annotations

import json
from dataclasses import dataclass
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fieldledger.agent.drafting import (
    ArtifactBlock,
    BlockDependency,
    build_block,
    canonical_json,
    draft_blocks,
)
from fieldledger.agent.extraction import (
    AgentSourceChunk,
    CitedFact,
    ExcludedClaim,
    classify_sources,
    extract_chunks,
    normalized_chunk_hash,
)
from fieldledger.agent.reconciliation import Contradiction, reconcile_facts
from fieldledger.agent.rules import RuleFinding, RuleSet, evaluate_rules
from fieldledger.domain.models import (
    ArtifactKind,
    ContractModel,
    DependencyKind,
    StageName,
    StageResult,
)
from fieldledger.domain.workflow import RunStatus, next_stage
from fieldledger.parsers.base import ParseOutcome, hash_bytes
from fieldledger.persistence.repositories import ClaimedStage, PreparedArtifact, PreparedDependency
from fieldledger.worker import StageExecution


class PileAnalysis(ContractModel):
    source_chunks: tuple[AgentSourceChunk, ...]
    classifications: dict[str, str]
    facts: tuple[CitedFact, ...]
    excluded_claims: tuple[ExcludedClaim, ...]
    contradictions: tuple[Contradiction, ...]
    findings: tuple[RuleFinding, ...]
    change_register_blocks: tuple[ArtifactBlock, ...]
    executive_brief_blocks: tuple[ArtifactBlock, ...]
    blocks: dict[str, ArtifactBlock]
    affected_block_keys: tuple[str, ...]
    execution_counts: dict[str, int]
    citation_coverage: float
    provider_calls: int = 0
    transitions_requested_by_sources: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ReuseContext:
    analysis: PileAnalysis | None
    mode: str
    predecessor_run_id: UUID | None


def source_chunks_from_parse(
    source_name: str, outcome: ParseOutcome
) -> tuple[AgentSourceChunk, ...]:
    if outcome.status != "parsed":
        return ()
    version_digest = hash_bytes(
        "|".join(chunk.content_hash for chunk in outcome.chunks).encode()
    )
    document_id = uuid5(NAMESPACE_URL, f"fieldledger-document:{source_name}")
    document_version_id = uuid5(document_id, version_digest)
    return tuple(
        AgentSourceChunk(
            source_name=source_name,
            document_id=document_id,
            document_version_id=document_version_id,
            source_chunk_id=uuid5(
                document_version_id, f"{chunk.ordinal}:{chunk.content_hash}"
            ),
            ordinal=chunk.ordinal,
            content=chunk.content,
            content_hash=chunk.content_hash,
            coordinate=chunk.coordinate,
        )
        for chunk in outcome.chunks
    )


def _normalized_chunks(chunks: tuple[AgentSourceChunk, ...]) -> tuple[AgentSourceChunk, ...]:
    return tuple(
        chunk.model_copy(update={"content_hash": normalized_chunk_hash(chunk.content)})
        if chunk.content_hash != normalized_chunk_hash(chunk.content)
        else chunk
        for chunk in chunks
    )


def _source_dependency(chunk: AgentSourceChunk) -> BlockDependency:
    return BlockDependency(
        kind=DependencyKind.SOURCE_VERSION,
        key=f"source-chunk:{chunk.source_chunk_id}",
        content_hash=chunk.content_hash,
    )


def _artifact_dependency(block: ArtifactBlock) -> BlockDependency:
    return BlockDependency(
        kind=DependencyKind.PROPOSAL,
        key=f"artifact:{block.block_key}",
        content_hash=block.content_hash,
    )


def _fact_groups(facts: tuple[CitedFact, ...]) -> dict[str, tuple[CitedFact, ...]]:
    grouped: dict[str, list[CitedFact]] = {}
    for fact in facts:
        grouped.setdefault(fact.fact_key, []).append(fact)
    return {
        key: tuple(sorted(members, key=lambda item: str(item.id)))
        for key, members in grouped.items()
    }


def _logical_chunk_key(chunk: AgentSourceChunk) -> tuple[UUID, str]:
    return (
        chunk.document_id,
        chunk.coordinate.model_dump_json(exclude_none=True),
    )


def analyze_pile(
    chunks: tuple[AgentSourceChunk, ...],
    raw_rules: dict[str, Any],
    *,
    previous: PileAnalysis | None = None,
) -> PileAnalysis:
    chunks = _normalized_chunks(chunks)
    current_by_id = {chunk.source_chunk_id: chunk for chunk in chunks}
    current_by_logical = {_logical_chunk_key(chunk): chunk for chunk in chunks}
    previous_by_logical = (
        {_logical_chunk_key(chunk): chunk for chunk in previous.source_chunks}
        if previous is not None
        else {}
    )
    content_changed_keys = {
        key
        for key in current_by_logical.keys() | previous_by_logical.keys()
        if key not in current_by_logical
        or key not in previous_by_logical
        or current_by_logical[key].content_hash != previous_by_logical[key].content_hash
    }
    extraction_changed_keys = content_changed_keys | {
        key
        for key in current_by_logical.keys() & previous_by_logical.keys()
        if current_by_logical[key].document_version_id
        != previous_by_logical[key].document_version_id
    }
    current_changed_ids = {
        current_by_logical[key].source_chunk_id
        for key in extraction_changed_keys
        if key in current_by_logical
    }
    previous_changed_ids = {
        previous_by_logical[key].source_chunk_id
        for key in extraction_changed_keys
        if key in previous_by_logical
    }
    current_sources = {chunk.source_name for chunk in chunks}
    previous_sources = (
        {chunk.source_name for chunk in previous.source_chunks}
        if previous is not None
        else set()
    )
    classification_changed_sources = current_sources ^ previous_sources
    classification_changed_sources.update(
        chunk.source_name
        for key in content_changed_keys
        for chunk in tuple(
            item
            for item in (current_by_logical.get(key), previous_by_logical.get(key))
            if item is not None
        )
        if "document type:" in chunk.content.lower()
    )

    if previous is None:
        classifications = classify_sources(chunks)
        facts, excluded = extract_chunks(chunks)
    else:
        classifications = dict(previous.classifications)
        refreshed = classify_sources(
            tuple(
                chunk
                for chunk in chunks
                if chunk.source_name in classification_changed_sources
            )
        )
        for source_name in classification_changed_sources:
            if source_name not in current_sources:
                classifications.pop(source_name, None)
        classifications.update(refreshed)
        retained_facts = tuple(
            fact
            for fact in previous.facts
            if fact.citation.source_chunk_id not in previous_changed_ids
        )
        retained_excluded = tuple(
            claim
            for claim in previous.excluded_claims
            if claim.citation.source_chunk_id not in previous_changed_ids
        )
        changed_facts, changed_excluded = extract_chunks(
            tuple(
                chunk
                for chunk in chunks
                if chunk.source_chunk_id in current_changed_ids
            )
        )
        facts = retained_facts + changed_facts
        excluded = retained_excluded + changed_excluded
    facts = tuple(sorted(facts, key=lambda item: (item.fact_key, str(item.id))))
    excluded = tuple(
        sorted(excluded, key=lambda item: (item.fact_key, str(item.citation.source_chunk_id)))
    )
    current_groups = _fact_groups(facts)
    previous_groups = _fact_groups(previous.facts) if previous is not None else {}
    changed_fact_keys = {
        key
        for key in current_groups.keys() | previous_groups.keys()
        if current_groups.get(key) != previous_groups.get(key)
    }

    if previous is None:
        contradictions = reconcile_facts(facts)
    else:
        previous_contradictions = {
            item.fact_key: item for item in previous.contradictions
        }
        contradiction_items: list[Contradiction] = []
        for fact_key in sorted(current_groups):
            if (
                fact_key not in changed_fact_keys
                and fact_key in previous_contradictions
            ):
                contradiction_items.append(previous_contradictions[fact_key])
            elif fact_key in changed_fact_keys:
                contradiction_items.extend(reconcile_facts(current_groups[fact_key]))
        contradictions = tuple(contradiction_items)

    if previous is None:
        findings = evaluate_rules(facts, raw_rules)
    else:
        parsed_rules = RuleSet.model_validate(raw_rules)
        previous_findings = {item.rule_id: item for item in previous.findings}
        finding_items: list[RuleFinding] = []
        for rule in parsed_rules.rules:
            prior = previous_findings.get(rule.id)
            unchanged = (
                prior is not None
                and rule.fact_key not in changed_fact_keys
                and prior.rule_version == parsed_rules.version
                and prior.fact_key == rule.fact_key
                and prior.operator == rule.operator
                and prior.expected == rule.expected
            )
            if unchanged and prior is not None:
                finding_items.append(prior)
            else:
                finding_items.extend(
                    evaluate_rules(
                        current_groups.get(rule.fact_key, ()),
                        {
                            "version": parsed_rules.version,
                            "rules": [rule.model_dump(mode="json")],
                        },
                    )
                )
        findings = tuple(finding_items)

    blocks: dict[str, ArtifactBlock] = {}
    previous_blocks = previous.blocks if previous is not None else {}
    chunks_by_source: dict[str, list[AgentSourceChunk]] = {}
    for chunk in chunks:
        chunks_by_source.setdefault(chunk.source_name, []).append(chunk)
    for source_name, classification in sorted(classifications.items()):
        key = f"classification:{source_name}"
        dependencies = tuple(
            _source_dependency(chunk)
            for chunk in sorted(chunks_by_source[source_name], key=lambda item: item.ordinal)
            if "document type:" in chunk.content.lower()
        )
        if (
            previous is not None
            and source_name not in classification_changed_sources
            and key in previous_blocks
        ):
            blocks[key] = previous_blocks[key].model_copy(
                update={"dependencies": dependencies}
            )
        else:
            blocks[key] = build_block(
                block_key=key,
                stage=StageName.CLASSIFY,
                kind=ArtifactKind.CLASSIFICATION,
                payload={"document_kind": classification, "source_name": source_name},
                dependencies=dependencies,
            )
    for fact in facts:
        key = f"fact:{fact.id}"
        if key in previous_blocks and fact.fact_key not in changed_fact_keys:
            blocks[key] = previous_blocks[key]
        else:
            blocks[key] = build_block(
                block_key=key,
                stage=StageName.EXTRACT,
                kind=ArtifactKind.FACT_SET,
                payload=fact.model_dump(mode="json", exclude_none=True),
                dependencies=(
                    _source_dependency(current_by_id[fact.citation.source_chunk_id]),
                ),
            )
    for claim in excluded:
        key = f"excluded:{claim.citation.source_chunk_id}:{claim.fact_key}"
        if (
            previous is not None
            and key in previous_blocks
            and claim in previous.excluded_claims
        ):
            blocks[key] = previous_blocks[key]
        else:
            blocks[key] = build_block(
                block_key=key,
                stage=StageName.EXTRACT,
                kind=ArtifactKind.EXCLUDED_CLAIMS,
                payload=claim.model_dump(mode="json", exclude_none=True),
                dependencies=(
                    _source_dependency(current_by_id[claim.citation.source_chunk_id]),
                ),
            )
    fact_by_id = {fact.id: fact for fact in facts}
    for contradiction in contradictions:
        dependencies = tuple(
            _artifact_dependency(blocks[f"fact:{fact_id}"])
            for fact_id in contradiction.fact_ids
            if fact_id in fact_by_id
        )
        key = f"contradiction:{contradiction.fact_key}"
        if contradiction.fact_key not in changed_fact_keys and key in previous_blocks:
            blocks[key] = previous_blocks[key]
        else:
            blocks[key] = build_block(
                block_key=key,
                stage=StageName.RECONCILE,
                kind=ArtifactKind.CONTRADICTION_SET,
                payload=contradiction.model_dump(mode="json"),
                dependencies=dependencies,
            )
    rule_hash = f"sha256:{sha256(canonical_json(raw_rules)).hexdigest()}"
    for finding in findings:
        dependencies = tuple(
            _artifact_dependency(blocks[f"fact:{fact_id}"])
            for fact_id in finding.fact_ids
        ) + (
            BlockDependency(
                kind=DependencyKind.RULE_SET_VERSION,
                key=f"rule-set:{finding.rule_version}",
                content_hash=rule_hash,
            ),
        )
        key = f"finding:{finding.rule_id}"
        prior_finding = (
            next(
                (item for item in previous.findings if item.rule_id == finding.rule_id),
                None,
            )
            if previous is not None
            else None
        )
        if prior_finding == finding and key in previous_blocks:
            blocks[key] = previous_blocks[key].model_copy(
                update={"dependencies": dependencies}
            )
        else:
            blocks[key] = build_block(
                block_key=key,
                stage=StageName.CHECK_RULES,
                kind=ArtifactKind.RULE_FINDINGS,
                payload=finding.model_dump(mode="json"),
                dependencies=dependencies,
            )

    current_contradictions = {item.fact_key: item for item in contradictions}
    previous_contradictions = (
        {item.fact_key: item for item in previous.contradictions}
        if previous is not None
        else {}
    )
    current_findings_by_key: dict[str, tuple[RuleFinding, ...]] = {}
    previous_findings_by_key: dict[str, tuple[RuleFinding, ...]] = {}
    current_excluded_by_key: dict[str, tuple[ExcludedClaim, ...]] = {}
    previous_excluded_by_key: dict[str, tuple[ExcludedClaim, ...]] = {}
    for fact_key in current_groups:
        current_findings_by_key[fact_key] = tuple(
            item for item in findings if item.fact_key == fact_key
        )
        previous_findings_by_key[fact_key] = (
            tuple(item for item in previous.findings if item.fact_key == fact_key)
            if previous is not None
            else ()
        )
        current_excluded_by_key[fact_key] = tuple(
            item for item in excluded if item.fact_key == fact_key
        )
        previous_excluded_by_key[fact_key] = (
            tuple(
                item for item in previous.excluded_claims if item.fact_key == fact_key
            )
            if previous is not None
            else ()
        )
    draft_changed_keys = {
        key
        for key in current_groups
        if key in changed_fact_keys
        or current_contradictions.get(key) != previous_contradictions.get(key)
        or current_findings_by_key[key] != previous_findings_by_key[key]
        or previous is None
        or current_excluded_by_key[key] != previous_excluded_by_key[key]
    }
    register_items: list[ArtifactBlock] = []
    brief_items: list[ArtifactBlock] = []
    for fact_key in sorted(current_groups):
        register_key = f"register:{fact_key}"
        brief_key = f"brief:{fact_key}"
        if (
            fact_key not in draft_changed_keys
            and register_key in previous_blocks
            and brief_key in previous_blocks
        ):
            register_items.append(previous_blocks[register_key])
            brief_items.append(previous_blocks[brief_key])
            continue
        register, brief = draft_blocks(
            current_groups[fact_key],
            tuple(
                item for item in contradictions if item.fact_key == fact_key
            ),
            current_findings_by_key[fact_key],
            current_excluded_by_key[fact_key],
            blocks,
        )
        register_items.extend(register)
        brief_items.extend(brief)
    register = tuple(register_items)
    brief = tuple(brief_items)
    for block in register + brief:
        blocks[block.block_key] = block

    affected = {
        key
        for key in blocks.keys() | previous_blocks.keys()
        if key not in blocks
        or key not in previous_blocks
        or blocks[key].content_hash != previous_blocks[key].content_hash
    }
    for key in blocks.keys() - affected:
        blocks[key] = blocks[key].model_copy(
            update={
                "canonical_bytes": previous_blocks[key].canonical_bytes,
                "content_hash": previous_blocks[key].content_hash,
            }
        )

    execution_counts = {
        f"extract:{key}": int(current_groups.get(key) != previous_groups.get(key))
        for key in sorted(current_groups.keys() | previous_groups.keys())
    }
    execution_counts.update(
        {
            f"classify:{source_name}": int(
                previous is None or source_name in classification_changed_sources
            )
            for source_name in classifications
        }
    )
    resolved = sum(fact.citation.resolves(chunks) for fact in facts)
    coverage = 1.0 if not facts else resolved / len(facts)
    return PileAnalysis(
        source_chunks=chunks,
        classifications=classifications,
        facts=facts,
        excluded_claims=excluded,
        contradictions=contradictions,
        findings=findings,
        change_register_blocks=tuple(blocks[item.block_key] for item in register),
        executive_brief_blocks=tuple(blocks[item.block_key] for item in brief),
        blocks=blocks,
        affected_block_keys=tuple(sorted(affected)),
        execution_counts=execution_counts,
        citation_coverage=coverage,
    )


class DeterministicStageExecutor:
    """Task 2 StageExecutor adapter backed only by immutable PostgreSQL inputs."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def __call__(self, claim: ClaimedStage) -> StageExecution:
        chunks, rules = await self._load_inputs(claim.run_id)
        reuse = await self._load_previous_analysis(claim.run_id)
        previous = reuse.analysis
        analysis = analyze_pile(chunks, rules, previous=previous)
        blocks = tuple(
            sorted(
                (block for block in analysis.blocks.values() if block.stage is claim.stage),
                key=lambda item: item.block_key,
            )
        )
        artifacts = tuple(
            PreparedArtifact(
                hash=block.content_hash,
                kind=block.kind,
                media_type="application/json",
                content=block.canonical_bytes,
            )
            for block in blocks
        )
        stage_keys = {block.block_key for block in blocks}
        previous_stage_keys = (
            {
                key
                for key, block in previous.blocks.items()
                if block.stage is claim.stage
            }
            if previous is not None
            else set()
        )
        executed = tuple(sorted(stage_keys & set(analysis.affected_block_keys)))
        reused = tuple(sorted(stage_keys - set(analysis.affected_block_keys)))
        removed = tuple(sorted(previous_stage_keys - stage_keys))
        evidence_content = canonical_json(
            {
                "stage": claim.stage.value,
                "executed_block_keys": executed,
                "reused_block_keys": reused,
                "removed_block_keys": removed,
                "reuse_mode": reuse.mode,
                "predecessor_run_id": (
                    str(reuse.predecessor_run_id)
                    if reuse.predecessor_run_id is not None
                    else None
                ),
            }
        )
        evidence = PreparedArtifact(
            hash=f"sha256:{sha256(evidence_content).hexdigest()}",
            kind=ArtifactKind.EXECUTION_EVIDENCE,
            media_type="application/json",
            content=evidence_content,
        )
        metadata: tuple[PreparedArtifact, ...] = (evidence,)
        if claim.stage is StageName.DRAFT:
            snapshot_content = canonical_json(
                {
                    "block_key": "analysis-snapshot",
                    "payload": analysis.model_dump(mode="json"),
                }
            )
            metadata += (
                PreparedArtifact(
                    hash=f"sha256:{sha256(snapshot_content).hexdigest()}",
                    kind=ArtifactKind.ANALYSIS_SNAPSHOT,
                    media_type="application/json",
                    content=snapshot_content,
                ),
            )
        artifacts += metadata
        dependencies = tuple(
            PreparedDependency(
                artifact_hash=block.content_hash,
                kind=dependency.kind,
                dependency_key=dependency.key,
                content_hash=dependency.content_hash,
            )
            for block in blocks
            for dependency in block.dependencies
        )
        return StageExecution(
            result=StageResult(
                status="completed",
                artifact_hashes=tuple(item.hash for item in artifacts),
                next_stage=next_stage(claim.stage),
            ),
            artifacts=artifacts,
            dependencies=dependencies,
            duration_ms=0,
        )

    async def _load_inputs(
        self, run_id: UUID
    ) -> tuple[tuple[AgentSourceChunk, ...], dict[str, Any]]:
        from fieldledger.persistence.tables import (
            document_versions,
            documents,
            rule_sets,
            run_input_manifest,
            source_chunks,
            workflow_runs,
        )

        async with self.sessions() as session:
            project_id = await session.scalar(
                select(workflow_runs.c.project_id).where(workflow_runs.c.id == run_id)
            )
            if project_id is None:
                raise LookupError(f"workflow run not found: {run_id}")
            manifest = (
                await session.execute(
                    select(
                        run_input_manifest.c.kind,
                        run_input_manifest.c.object_id,
                        run_input_manifest.c.content_hash,
                    ).where(run_input_manifest.c.workflow_run_id == run_id)
                )
            ).all()
            version_refs = {
                row.object_id: row.content_hash
                for row in manifest
                if row.kind == DependencyKind.SOURCE_VERSION.value
            }
            rule_refs = {
                row.object_id: row.content_hash
                for row in manifest
                if row.kind == DependencyKind.RULE_SET_VERSION.value
            }
            rows = (
                await session.execute(
                    select(
                        documents.c.name,
                        documents.c.id.label("document_id"),
                        document_versions.c.id.label("version_id"),
                        document_versions.c.content_hash.label("version_hash"),
                        source_chunks.c.id.label("chunk_id"),
                        source_chunks.c.ordinal,
                        source_chunks.c.content,
                        source_chunks.c.content_hash,
                        source_chunks.c.coordinate,
                    )
                    .select_from(
                        documents.join(document_versions).join(source_chunks)
                    )
                    .where(documents.c.project_id == project_id)
                    .where(document_versions.c.id.in_(tuple(version_refs)))
                    .order_by(documents.c.name, source_chunks.c.ordinal)
                )
            ).mappings().all()
            rule_rows = (
                await session.execute(
                    select(rule_sets.c.id, rule_sets.c.content_hash, rule_sets.c.rules)
                    .where(rule_sets.c.project_id == project_id)
                    .where(rule_sets.c.id.in_(tuple(rule_refs)))
                )
            ).all()
        loaded_versions = {row["version_id"]: row["version_hash"] for row in rows}
        if loaded_versions != version_refs:
            raise RuntimeError(f"run {run_id} has an invalid source input manifest")
        loaded_rules = {row.id: row.content_hash for row in rule_rows}
        if loaded_rules != rule_refs:
            raise RuntimeError(f"run {run_id} has an invalid rule input manifest")
        if len(rule_rows) > 1:
            raise RuntimeError(f"run {run_id} has multiple rule-set inputs")
        chunks = tuple(
            AgentSourceChunk(
                source_name=row["name"],
                document_id=row["document_id"],
                document_version_id=row["version_id"],
                source_chunk_id=row["chunk_id"],
                ordinal=row["ordinal"],
                content=row["content"],
                content_hash=row["content_hash"],
                coordinate=row["coordinate"],
            )
            for row in rows
        )
        raw_rules = rule_rows[0].rules if rule_rows else None
        return chunks, dict(raw_rules or {"version": "no-rules", "rules": []})

    async def _load_previous_analysis(self, run_id: UUID) -> _ReuseContext:
        from fieldledger.persistence.tables import (
            artifact_blobs,
            stage_artifacts,
            workflow_runs,
        )

        async with self.sessions() as session:
            current = (
                await session.execute(
                    select(workflow_runs.c.supersedes_run_id).where(
                        workflow_runs.c.id == run_id
                    )
                )
            ).one_or_none()
            if current is None:
                raise LookupError(f"workflow run not found: {run_id}")
            predecessor = current.supersedes_run_id
            if predecessor is None:
                return _ReuseContext(None, "no_predecessor", None)
            predecessor_row = (
                await session.execute(
                    select(workflow_runs.c.status, workflow_runs.c.failure).where(
                        workflow_runs.c.id == predecessor
                    )
                )
            ).one_or_none()
            if predecessor_row is None:
                return _ReuseContext(None, "predecessor_absent", predecessor)
            if (
                predecessor_row.status == RunStatus.REVIEW_READY.value
                and predecessor_row.failure is not None
            ):
                return _ReuseContext(None, "predecessor_failed", predecessor)
            content = await session.scalar(
                select(artifact_blobs.c.content)
                .select_from(stage_artifacts.join(artifact_blobs))
                .where(stage_artifacts.c.workflow_run_id == predecessor)
                .where(
                    stage_artifacts.c.kind
                    == ArtifactKind.ANALYSIS_SNAPSHOT.value
                )
                .order_by(stage_artifacts.c.created_at.desc())
                .limit(1)
            )
            if content is None:
                raise RuntimeError(
                    f"predecessor {predecessor} has no reusable analysis snapshot"
                )
            payload = json.loads(content)
            return _ReuseContext(
                PileAnalysis.model_validate(payload["payload"]),
                "predecessor_snapshot",
                predecessor,
            )
