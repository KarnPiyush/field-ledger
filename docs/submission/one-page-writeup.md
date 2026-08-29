# FieldLedger: Cited Change Control That Keeps Working Offline

Construction teams make consequential decisions from contracts, change orders, field reports, invoices, and rules that rarely agree cleanly. FieldLedger turns that pile into a reviewable change register and executive brief while preserving the evidence needed to trust each fact.

The workflow is deliberately explicit. It classifies sources, extracts facts with exact coordinates, reconciles contradictions, checks versioned rules, and drafts content-addressed blocks. PostgreSQL is the durable authority. Leases and fencing prevent late workers from committing; idempotency prevents duplicate mutation; immutable run manifests make every review reproducible. When one source changes, dependency edges identify only the downstream work that must run again.

Review stays human. The phone PWA shows one change, its reason, stacked before/after content, and the cited source. Decisions can be saved in IndexedDB without signal. The ordered queue checks the complete reviewed dependency vector before every send and stops at stale, failed, or ambiguous work. The Word task pane uses the same queue semantics but stores commands in document settings and fingerprints the document and selection. It creates a proposal; it never silently edits Word.

REST and stdio MCP use the same domain services and project authorization. MCP can inspect and relay a UI-confirmed decision, but it cannot mint its own confirmation or cross the configured project boundary. The optional SuperDocs adapter stays server-side. The default deterministic adapter needs no API key, makes no network request, and records zero provider cost.

Proof is part of the product. A coherent synthetic pile has six published facts with 100% exact citation coverage, one contradiction, two failed rules, and an unsupported savings claim that remains excluded. Seven measured deterministic runs recorded a 20.477 ms p50 and 26.645 ms nearest-rank p95 on the documented host. Browser suites cover 320/390/768 phone layouts, offline reload, stale stop, lost response, async completion, and the 330 px Word pane. Operator commands start, diagnose, verify, measure, inspect MCP, reset only confirmed synthetic state, and create an audited allowlist ZIP.

This submission is intentionally local. No live SuperDocs request was made, no remote Git endpoint exists, and no service, video, or archive was transferred externally. Native Word sideload and truly uncached image-pull timing are reported as unverified rather than implied.

See the [requirement matrix](requirement-matrix.md), [measurements](measurement.md), [required answers](required-answers.md), and [demo narration](../demo/narration-script.md).
