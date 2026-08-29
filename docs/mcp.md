# MCP Interface

FieldLedger exposes a local stdio MCP server backed by the same domain services as REST.

```powershell
pwsh ./scripts/mcp-inspector.ps1
```

POSIX: `sh ./scripts/mcp-inspector.sh`.

For a noninteractive entrypoint check:

```powershell
pwsh ./scripts/mcp-inspector.ps1 --smoke
```

POSIX: `sh ./scripts/mcp-inspector.sh --smoke`.

## Tools

- `create_project`: create the allowed local project.
- `add_fixture_source`: ingest one named synthetic fixture.
- `start_run`: start an idempotent workflow.
- `inspect_run`: inspect run status and current stage.
- `list_pending_reviews`: list reviewable batches.
- `request_human_confirmation`: return `ui_action_required`; it cannot mint confirmation itself.
- `submit_review_decisions`: relay UI-confirmed decisions through the review service.
- `inspect_audit`: return bounded project audit history.
- `export`: export an approved document artifact.

## Authority Boundary

The local MCP actor is bound to one configured project. Cross-project identifiers are rejected. Tool output and source text cannot grant roles, mint a UI confirmation, approve an agent's own proposal, or bypass dependency checks. The original idempotency key survives a lost response. No live provider credential is needed for inspection or the deterministic demo.
