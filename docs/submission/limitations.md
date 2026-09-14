# Assumptions, Security, And Limitations

## Assumptions And Decisions

- PostgreSQL is reachable wherever durable workflows run.
- Source versions are immutable after ingestion.
- The target document, cited sources, rules, policy/prompt, and proposal all participate in freshness checks.
- A phone or Word queue is a durable client buffer, never the authority for an applied outcome.
- Deterministic mode is the supported evaluation path; optional provider behavior is isolated behind a server adapter.

## Security And Privacy Boundary

- Source text is inert data and cannot invoke tools or change authority.
- Signed sessions and durable project membership protect reads and writes.
- Human confirmation is issued only from the UI and binds actor, batch, payload, and expiry.
- REST and MCP can relay but cannot self-approve.
- Provider credentials, when used outside this submission, remain server-side and outside tracked files.
- Logs use correlation identifiers and error classes instead of environment values or document content.
- Package creation scans allowlisted raw content plus extractable DOCX/PDF content before and after ZIP creation, and rejects unsafe paths, private host paths, and known credential patterns.

The system is not a redaction product. Synthetic demo files are the only data recorded in screenshots and video.

## Known Limitations

- **Current fixture blocker (14 September 2026):** `apps/api/tests/fixtures/contract.pdf` and `demo/fixtures/construction-contract.pdf` have SHA-256 `bb6af8dd09480f91de8d8d6d63549e47db391ecff98386e1bd0e4a1b66f29eee`, but the delivery manifest and demo oracle expect `9f97df18a5ed55361ad5b582c37482a09cf512fa1139ef560655769dd75db251`. The remote baseline contains the same bytes, so this is not a documentation-edit regression. Canonical fixture seeding and package hash checks fail until the intended fixture is recovered and its citations revalidated. Do not bypass integrity checks or merely change the approved hash to make them green.

- Native Word desktop sideload was not part of automated verification. Office bridge tests and browser-host tests do not replace a named desktop-host persistence/replay check.
- No live SuperDocs request was made, so live latency, cost, rate limits, and UI compatibility are unverified.
- Scanned/image-only PDFs need OCR before the current parser can cite text.
- PDF coordinates are page-level; DOCX coordinates are paragraph or table-cell level. Pixel bounding boxes are not produced.
- The deterministic extractor recognizes the fixture's typed fact patterns; it is an evaluation adapter, not a claim of broad language understanding.
- The recorded PWA uses an authenticated deterministic seed through the real local API. Production authentication/session provisioning still needs deployment-specific configuration.
- There is no DMS connector, native signature workflow, multiplayer editing, guaranteed redaction, automatic conflict overwrite, or decision carry-forward across versions.
- True cold container-pull time was unavailable on the cached host and is not claimed.
- Python and Node application images use constrained floating base tags, so an image rebuilt later can receive upstream OS/runtime patch changes. Python/Node dependencies are lockfile-frozen and the stateful PostgreSQL image is digest-pinned, but full application-image bit reproducibility is not claimed.
- The verified browser matrix is Chromium at phone widths 320/390/768 and desktop. Safari, Firefox, mobile OS background sync, Word for macOS, and Office on the web are unverified.

## Delivery Boundary

Source and included artifacts are published in the private `KarnPiyush/field-ledger` repository. Reviewer access must be arranged separately. Repository publication does not prove hosting, a public contribution, live provider acceptance, or form/Drive submission. ZIP generation remains a separate checked operation.

## Source-verified Recovery Qualifications

These qualifications were checked against the submitted source during the documentation review. They are not live SuperDocs bug reports and are not fixed by this documentation change.

- **Phone replay identity:** [MobileQueueSender](../../apps/mobile/src/api/queueSender.ts) requests a fresh confirmation in both send and reconcile. [ReviewService._request_hash](../../apps/api/src/fieldledger/services/review_service.py) hashes the full submission, including that token. Reusing the command key with a new token can conflict with the original request. Exact backend replay tests do not establish recovery for this changed-token client path.
- **Early stale errors:** a confirmation-request failure goes through `beforeDispatchFailure`, not the decision-submission stale mapper. The server can reject stale inputs safely while the phone shows a generic fetch error instead of changed-source detail.
- **Provider continuation:** the [SuperDocs adapter](../../apps/api/src/fieldledger/providers/superdocs.py) recognizes a continuation pause but has no continuation request implementation. Approval support is not a complete implementation of every provider lifecycle.
- **Bounded wait:** phone polling waits eight increasing intervals (nine seconds total, plus network time). Timeout or a lost response is not proof of remote rollback, even where current UI text says nothing was applied.
- **Browser and Office storage:** reload persistence is tested, but clearing site data, eviction, quota failures and every native host's save behavior are not universal durability guarantees.
- **Provider transaction boundary:** database transactions cannot undo remote mutations. Recovery depends on authoritative reads/provider receipts, and some provider calls hold database locks. Production latency, concurrency and unknown-outcome recovery need live validation.

For the actual sequence and state meanings, see [the walkthrough](../user-flow.md) and [architecture](../architecture.md).
