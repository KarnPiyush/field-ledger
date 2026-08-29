# Requirement Matrix

| Requirement | Implementation | Proof |
|---|---|---|
| Cited document-pile workflow | `apps/api/src/fieldledger/agent/`, `services/ingestion_service.py` | `apps/api/tests/eval/test_agent_golden.py`; `demo/video-proof/frame-030.png` |
| Exact citation and unsupported exclusion | `demo/fixtures/oracle.json` | `apps/api/tests/delivery/test_submission_delivery.py`; 100% rows in `demo/measurements/workflow-raw.csv` |
| Restart, fencing, idempotency | `worker.py`, `persistence/repositories.py`, `domain/workflow.py` | `apps/api/tests/integration/test_workflow_persistence.py`; architecture diagram |
| Focused recomputation | `agent/stages.py`, `services/invalidation_service.py` | `test_agent_golden.py`, `test_ingestion_review_fixes.py`; measurement unchanged-block column |
| Secure human review | `services/review_service.py`, `security.py`, `services/mutation_fence.py` | `test_review_authority.py`, `test_security_review.py` |
| REST contract | `apps/api/openapi.json`, `packages/api-client/` | `pnpm api:check`; [API reference](../api-reference.md) |
| MCP tools and authority | `apps/api/src/fieldledger/mcp_server.py` | `test_mcp_server.py`; [MCP docs](../mcp.md) |
| Offline phone approvals | `apps/mobile/`, `packages/offline-queue/` | `apps/mobile/tests/mobile-flows.spec.ts`; gated `apps/mobile/tests/compose-integration.spec.ts`; `demo/video-proof/frame-055.png` |
| Stale source blocks queue | shared drain and mobile conflict UI | `packages/offline-queue/src/fingerprint.test.ts`; `apps/mobile/src/api/queueSender.test.ts`; real-409 case in `mobile-flows.spec.ts` |
| Word offline instruction queue | `extensions/word/` | `extensions/word/tests/word-queue.spec.ts`; gated `extensions/word/tests/compose-integration.spec.ts`; `extensions/word/screenshots/word-taskpane-conflict-330.png` |
| Async completion safety | phone and Word `asyncPhase` plus provider lifecycle | `apps/mobile/tests/async-completion.spec.ts`; `extensions/word/src/data/replay.test.ts`; `test_provider_lifecycle.py` |
| Operator commands | `scripts/delivery.py` and paired wrappers | delivery tests; [operations](../operations.md) |
| Layman and developer diagrams | [user flow](../user-flow.md), [architecture](../architecture.md) | real Mermaid renderer through `validate-mermaid` |
| Required assignment answers | [required answers](required-answers.md) | four numbered sections in order |
| About ten buyer use cases | [use cases](use-cases.md) | ten role-specific rows without outreach claim |
| Reproducible measurement | `scripts/measure-setup.*`, `demo/measurements/` | summary recomputation test; [measurement](measurement.md) |
| Three-to-five-minute demo | `demo/fieldledger-superdocs-demo.mp4` | media probe test and [narration](../demo/narration-script.md) |
| Local Git and ZIP only | package/reset allowlists | package safety test, empty `git remote -v`, [limitations](limitations.md) |
