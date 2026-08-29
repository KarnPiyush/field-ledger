# FieldLedger User Flow

## In Plain Language

1. Put the contract, change order, field report, invoice, and review rules into one project.
2. FieldLedger reads each file, finds facts, and keeps an exact pointer back to the source page, paragraph, table cell, or line.
3. It reports contradictions and rule failures. It does not turn unsupported text into a fact.
4. A reviewer opens one change at a time on the phone, checks the evidence, and approves or rejects it. A rejection needs a note.
5. With no signal, decisions stay on that phone in order. The screen says **Saved on this phone**, not **Applied**.
6. On reconnect, FieldLedger checks every reviewed dependency before each command. If a cited source changed, the queue stops and nothing is applied.
7. A successful decision creates an audit record. Proposal work can be reviewed and exported only after human confirmation.
8. In Word, an instruction is stored inside document settings while offline. It creates a proposal on reconnect and never silently edits the document.

## End-to-End Flow

```mermaid
flowchart TD
  A[Construction document pile] --> B[Classify and extract]
  B --> C[Resolve exact citations]
  C --> D[Find conflicts and apply rules]
  D --> E[Draft change register and brief]
  E --> F[Human review on phone]
  F -->|offline| G[Ordered local queue]
  G --> H{Dependencies unchanged?}
  H -->|no| I[Stop: nothing applied]
  H -->|yes| J[Apply once]
  J --> K[Proposal review and export]
  K --> L[Audit proof]
  M[Word instruction] --> N[Document settings queue]
  N --> H
```

## Mixed Offline Decisions

```mermaid
sequenceDiagram
  actor Reviewer
  participant Phone
  participant Queue
  participant API
  participant DB as PostgreSQL
  participant Word
  Reviewer->>Phone: Approve item 1
  Reviewer->>Phone: Reject item 2 with note
  Phone->>Queue: Save each item draft locally
  Phone->>Queue: Enqueue one ordered five-decision command
  Note over Phone,Queue: Network can disappear and the queue survives reload
  Queue->>API: Reconnect and preflight the full batch
  API->>DB: Compare full dependency vector
  alt cited source changed
    DB-->>API: Stale plus changed source
    API-->>Queue: Stop before mutation
    Queue-->>Reviewer: Nothing was applied, refresh review
  else unchanged
    API->>DB: Commit idempotent decision and audit
    DB-->>Queue: Applied once
  end
  Reviewer->>Word: Queue instruction offline
  Word->>Queue: Persist in document settings
  Queue->>API: Submit as proposal after preflight
  API-->>Reviewer: Open proposal review
```

Status words are evidence: **pending** means local, **sending** means one command is in flight, **applied** means the server confirmed it, and **needs reconciliation** means the result must be checked before continuing.
