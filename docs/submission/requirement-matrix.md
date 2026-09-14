# Assignment and Evidence Map

The engineer brief has **four task sections**, not four assigned builds. Task 1 is the common document analyst; Task 2 includes the email-assigned **Offline-capable review and approval client for a phone**; Task 3 is use cases and prospective buyers; Task 4 is video and write-up with architecture. The Word companion is an optional additional build, not a second email assignment.

This is a navigation map, not a blanket completion claim. Tests below are runnable evidence locations; historical results do not prove current live-provider behavior. Start with [the walkthrough](../user-flow.md) and [architecture](../architecture.md).

| Area | Implementation / artifact | Verification and qualification |
|---|---|---|
| Task 1: cited analysis | [stages](../../apps/api/src/fieldledger/agent/stages.py), [ingestion](../../apps/api/src/fieldledger/services/ingestion_service.py) | [golden evaluation](../../apps/api/tests/eval/test_agent_golden.py); typed-pattern extraction, not demonstrated general adaptive analysis or vector retrieval |
| Citations and unsupported claims | [fixture oracle](../../demo/fixtures/oracle.json) | [delivery checks](../../apps/api/tests/delivery/test_submission_delivery.py); fixture citation coverage is not an accuracy claim for arbitrary files |
| Restart-safe workflow | [worker](../../apps/api/src/fieldledger/worker.py), [repositories](../../apps/api/src/fieldledger/persistence/repositories.py) | [persistence tests](../../apps/api/tests/integration/test_workflow_persistence.py) |
| Focused recomputation | [invalidation](../../apps/api/src/fieldledger/services/invalidation_service.py) | [ingestion/review regression tests](../../apps/api/tests/integration/test_ingestion_review_fixes.py), [measurements](measurement.md) |
| Authorized human review | [review service](../../apps/api/src/fieldledger/services/review_service.py) | [authority tests](../../apps/api/tests/integration/test_review_authority.py), [security tests](../../apps/api/tests/unit/test_security_review.py); production identity remains deployment-specific |
| REST and MCP | [OpenAPI](../../apps/api/openapi.json), [API docs](../api-reference.md), [MCP docs](../mcp.md) | `pnpm api:check`, [MCP contract tests](../../apps/api/tests/contract/test_mcp_server.py) |
| Task 2: assigned phone client | [phone source](../../apps/mobile/src/app/App.tsx), [queue store](../../apps/mobile/src/data/queueStore.ts) | [browser flows](../../apps/mobile/tests/mobile-flows.spec.ts), [Compose integration](../../apps/mobile/tests/compose-integration.spec.ts); offline reload and one mixed batch are deterministic scenarios |
| Ordered replay and freshness | [drain](../../packages/offline-queue/src/drain.ts), [phone sender](../../apps/mobile/src/api/queueSender.ts) | [queue tests](../../packages/offline-queue/src/drain.test.ts), [sender tests](../../apps/mobile/src/api/queueSender.test.ts); fresh-token replay and early-stale UX gaps remain in [limitations](limitations.md) |
| SuperDocs integration | [server adapter](../../apps/api/src/fieldledger/providers/superdocs.py), [provider lifecycle](../../apps/api/src/fieldledger/services/provider_service.py) | [lifecycle tests](../../apps/api/tests/integration/test_provider_lifecycle.py); no live acceptance run, incomplete continuation handling |
| Optional Word build | [Word replay](../../extensions/word/src/data/replay.ts), [sideload guide](../word-sideload.md) | [browser queue tests](../../extensions/word/tests/word-queue.spec.ts), [Compose integration](../../extensions/word/tests/compose-integration.spec.ts); proposal handoff only, native-host verification not claimed |
| Async outcomes | [phone sender](../../apps/mobile/src/api/queueSender.ts) | [async completion tests](../../apps/mobile/tests/async-completion.spec.ts); timeout is not proof of remote rollback |
| Task 3: use cases and buyers | [use cases](use-cases.md) | Ten role/company-type rows exist; this is not the requested named-company research. No outreach is claimed |
| Task 4: explanation and architecture | [write-up](one-page-writeup.md), [one-page architecture](../architecture-one-page.md), [required answers](required-answers.md) | Readable source and Mermaid diagrams; limitations qualify original completion claims |
| Task 4: demonstration | [video](../../demo/fieldledger-superdocs-demo.mp4), [recorded narration](../../demo/narration.txt) | Original deterministic recording; external video/form/Drive delivery and reviewer access not established here |
| Local operation and packaging | [operations](../operations.md), [delivery manifest](../../scripts/delivery-manifest.json) | Delivery tests check inventory, links, fixture hashes, media and packaging; rerun before producing a new ZIP |
| Publication | [repository](https://github.com/KarnPiyush/field-ledger) | Private repository as of 14 September 2026; not evidence of a hosted app or public upstream contribution |

## Run the relevant checks

From the repository root after installing the locked dependencies:

```powershell
uv sync --frozen
pnpm install --frozen-lockfile
uv run pytest apps/api/tests/delivery/test_submission_delivery.py -q
uv run python scripts/delivery.py validate-mermaid --json
```

The delivery suite includes packaging and fixture checks, so a failure there may not be a documentation-link failure. [Operations](../operations.md) describes the full verifier and the browser/Compose gates; those must be run to make fresh end-to-end claims.
