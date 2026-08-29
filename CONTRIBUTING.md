# Contributing

FieldLedger uses Python 3.12 through `uv`, Node 22, and pnpm 11. Create a local branch, keep credentials outside tracked files, and preserve the deterministic provider as the default.

1. Run `pwsh ./scripts/doctor.ps1`.
2. Write a failing focused test and confirm the expected failure.
3. Implement the smallest behavior that passes it.
4. Run `pwsh ./scripts/verify.ps1 --fast` while iterating.
5. Run the complete `pwsh ./scripts/verify.ps1` before integration.

Human confirmation authority must remain in the UI. Queue ordering, idempotency, stale checks, fencing, and audit history are security and data-integrity contracts, not optional conventions.
