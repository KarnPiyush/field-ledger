# FieldLedger Architecture: One Page

```mermaid
flowchart TB
  Sources[Construction sources and rules] --> Ingest[Immutable versions and citations]
  Ingest --> Stages[Deterministic analysis stages]
  Stages --> Artifacts[Cited register and brief / pinned run]
  Artifacts --> Edit[Separate provider edit request]
  Edit --> Provider[Deterministic adapter or optional SuperDocs]
  Provider --> Review[Pending change review batch]
  Review --> Phone[Phone cached review and IndexedDB drafts]
  Phone --> Queue[One ordered batch command]
  Queue --> Guard[Confirmation and freshness checks]
  Guard -->|changed before dispatch| Stale[Stop and refresh review]
  Guard -->|valid| Apply[Provider approval and authoritative read]
  Apply -->|confirmed| Result[Result and audit / further review or export]
  Apply -->|unknown| Reconcile[Stop and reconcile / do not assume rollback]
  Word[Word instruction in Office settings] --> Fingerprint[Check document and selection fingerprints]
  Fingerprint --> Edit
  DB[(PostgreSQL authority)] --- Ingest
  DB --- Artifacts
  DB --- Review
  DB --- Result
```

**Authority:** PostgreSQL; client storage is an offline buffer. **Worker recovery:** leases, fencing and transactional checkpoints. **Review safety:** authorization, reviewed dependency hashes, short-lived confirmation and request identity checks. **Provider:** no-key deterministic mode by default; optional SuperDocs credentials stay server-side. **Word:** proposal handoff, not automatic native write-back. **Unknown outcome:** reconciliation, not proof that nothing happened.

Read the [code map and transaction boundaries](architecture.md), [walkthrough](user-flow.md), and [known limitations](submission/limitations.md). The repository is published; hosting, live SuperDocs acceptance and native Word round-trip are not established by publication.
