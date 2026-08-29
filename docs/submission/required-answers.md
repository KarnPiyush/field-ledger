# Required Answers

## 1. What Broke Or Was Confusing?

### Observed Live SuperDocs Product Issues

None are claimed. This submission did not send a request to the live SuperDocs product or use a live credential. It would be misleading to present local adapter findings as live-product bugs.

### SuperDocs Integration Rough Edges Found From The Published Contract

- Async work can return several reviewable spellings, including `PreviewReady`, instead of one terminal shape. A client that waits only for `completed` can remain loading forever.
- A successful mutation response is not enough when the response can be lost. The integration needs a durable provider job identifier, idempotency key, and follow-up read before it can claim success.
- Upload/edit ownership needs an exclusive provider session per document. Reusing a provider item without lifecycle reconciliation risks edits against the wrong document version.
- Error, timeout, cancellation, stream failure, and post-mutation refresh are distinct completion paths. Each needs explicit UI recovery and a cleared loading state.

These were handled in the server adapter and tested with fake transports. They are integration risks, not evidence of defects observed against the live service.

### Local Implementation Issues Found And Fixed

- Early Compose web services returned placeholder text instead of the actual phone and Word builds. They now build and serve the real interfaces.
- The first review implementation needed stronger project-membership checks, replay binding, durable confirmation reservations, and mutation fences. Those controls are now enforced by focused security and persistence tests.
- Incremental recomputation originally risked carrying invalidation onto the wrong lineage and serializing too much work. Successor-only invalidation and byte-identical unaffected blocks now have regression tests.
- Browser and Office-adapter verification are complete. Native Word desktop persistence/replay remains explicitly unverified rather than inferred from those tests.
- The host started without a full FFmpeg installation. A user-scoped tool was installed only for local video creation/probing and is not packaged.
- A genuinely uncached container pull could not be reproduced without destroying shared host caches. The report records cached setup samples and marks cold pull unavailable.

## 2. The One Number I Would Watch Every Morning

**Median time to a trusted review batch:** elapsed time from a source version arriving to the first human-openable batch in which every published fact has a resolving citation and no stale dependency.

This metric combines customer value, latency, evidence quality, and freshness. A fast but uncited batch is unsafe; a fully cited batch that arrives after the decision window is useless. I would segment it by project size and source format. Supporting guardrails are citation coverage, stale-stop rate, queue age, duplicate-apply count, unresolved reconciliation count, and p95 stage duration.

## 3. Five Features To Build Next

1. **Native Word sideload and proposal round-trip:** close the largest remaining proof gap across desktop Word versions.
2. **Reviewer-ready document viewer:** render PDF/DOCX evidence at the exact coordinate with permission-aware download and redaction controls.
3. **Project connectors:** monitored SharePoint/Drive/DMS folders with immutable ingestion receipts and explicit tenant authorization.
4. **Rule authoring and simulation:** let operations teams test a versioned rule against historical projects before activation.
5. **Review analytics:** explain time-to-trusted-review, stale causes, queue age, and recurring source conflicts without exposing document content.

To make room, I would drop real-time multiplayer cursors, generic chat, automatic conflict overwrite, broad native mobile work, and custom model training. The immediate fixes stay ahead of new scope: normalize provider statuses, preserve job/idempotency linkage, make every loading path recover, improve exact evidence navigation, and complete native Word persistence testing.

## 4. Running Development And GTM As An Agent System

A small human team sets objectives, risk budgets, and customer promises. Agents run the repeatable loops:

1. **Demand loop:** agents summarize support calls and opt-in product telemetry into ranked problems; a product lead approves the weekly problem list. No agent sends outreach without an approved audience, message, and sending gate.
2. **Delivery loop:** one agent writes a code-aware spec, implementation agents work from failing tests, and a final independent review runs security, architecture, design, DX, and browser QA together. Humans approve schema changes, authority-boundary changes, releases, and customer data access.
3. **Reliability loop:** agents watch stage duration, citation coverage, stale rate, queue age, reconciliation backlog, and canary errors. They reproduce and propose fixes; humans approve high-blast-radius actions.
4. **GTM loop:** agents research named accounts from authorized sources, prepare role-specific demos and drafts, and measure funnel steps. Humans own positioning, pricing exceptions, legal claims, and final contact.
5. **Learning loop:** every incident, lost deal, and evaluation failure becomes a regression fixture or explicit decision record. Weekly pruning prevents contradictory instructions from accumulating.

The first likely failures are metric gaming, duplicated agent work, stale product context, accidental authority expansion, hidden customer-data leakage, and automation that reports activity instead of outcomes. Durable task ownership, least privilege, provenance, idempotency, budget caps, evaluation suites, and named human gates address those risks. This can produce the throughput of a much larger team without pretending that judgment and accountability are automated away.
