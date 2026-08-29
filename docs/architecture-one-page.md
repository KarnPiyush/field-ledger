# FieldLedger Architecture: One Page

```mermaid
flowchart TB
  Sources[Construction pile] --> Ingest[Immutable versions and cited chunks]
  Ingest --> Stages[Classify > Extract > Reconcile > Check rules > Draft]
  Stages --> Review[Human review batch]
  Review --> Phone[Phone offline decisions]
  Review --> Word[Word offline instructions]
  Phone --> Guard[Dependency preflight and ordered drain]
  Word --> Guard
  Guard -->|unchanged| Commit[Idempotent apply and audit]
  Guard -->|changed or unknown| Stop[Stale or reconcile: nothing applied]
  Commit --> Proposal[Proposal review, export, audit]
  DB[(PostgreSQL)] --- Ingest
  DB --- Stages
  DB --- Review
  DB --- Commit
```

**Authority:** PostgreSQL, never browser storage. **Recovery:** leases, heartbeats, fencing tokens, idempotency keys, content-addressed outputs, and startup reclaim. **Trust:** exact citations and dependency vectors; source text cannot invoke tools. **Human gate:** UI confirmation is required for mutation. **Offline:** Dexie on phone and Office document settings in Word share one pure queue contract. **Provider:** deterministic/no-key by default; optional SuperDocs access stays server-only. **Delivery:** local Git and allowlisted ZIP, with no remote transfer.
