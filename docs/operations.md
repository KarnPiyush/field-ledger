# Operations

## Start

```powershell
pwsh ./scripts/demo.ps1
```

The command checks Docker, Compose, the configured database/API/mobile/Word ports, 2 GB free space, and required files. It forces deterministic provider mode, blanks the provider key even when a project `.env` contains one, generates local signing/bootstrap secrets, starts all services, runs migrations, and waits on environment-derived API/mobile/Word URLs. UI fixture seeding is idempotent, so a second run keeps existing review work.

POSIX equivalent: `sh ./scripts/demo.sh`.

## Diagnose

```powershell
pwsh ./scripts/doctor.ps1
pwsh ./scripts/doctor.ps1 --running
```

POSIX: `sh ./scripts/doctor.sh` or `sh ./scripts/doctor.sh --running`.

`--running` additionally checks all six services and their readiness endpoints. Failed checks include a concrete recovery action. Configuration values are never printed.

Useful recovery commands:

```powershell
docker compose ps
docker compose logs api db
docker compose exec -T api alembic -c apps/api/alembic.ini current
```

## Verify

```powershell
pwsh ./scripts/verify.ps1
```

POSIX: `sh ./scripts/verify.sh`.

Default verification runs Python lint/type/tests, Node lint/type/unit/property tests, API drift, production builds, mobile and Word Playwright, Mermaid rendering, video probe, package-contract/secret checks, Compose resolution, and container builds. `--fast` skips browser suites and container image builds for iteration only.

## Reset Synthetic State

```powershell
pwsh ./scripts/reset.ps1 --confirm RESET-SYNTHETIC-DATA
```

POSIX: `sh ./scripts/reset.sh --confirm RESET-SYNTHETIC-DATA`.

Reset accepts only the exact resolved `.data` path and requires its `.fieldledger-synthetic-state` marker before deletion. It stops local Compose services and removes the named volume first; if that command fails, `.data` is preserved. An absent or misspelled confirmation, another repository directory, an outside path, or a missing marker is refused.

## Measurements

```powershell
pwsh ./scripts/measure-setup.ps1 --samples 7
```

POSIX: `sh ./scripts/measure-setup.sh --samples 7`.

The command writes timestamped setup rows and deterministic workflow rows under `demo/measurements/`, then recomputes the checked summary. It labels cached conditions and does not claim an unavailable cold image pull.

## Re-record The Walkthrough

Start from reset synthetic state so the recorder can prove an applied-audit delta rather than replay old server state:

```powershell
pwsh ./scripts/reset.ps1 --confirm RESET-SYNTHETIC-DATA
pwsh ./scripts/demo.ps1
pwsh ./demo/record-demo.ps1
```

The recorder uses the already-running API, phone, and Word Compose services; it does not start or stop substitute preview servers. It fails unless the phone sends one five-item decision batch, reconnect produces exactly one new applied audit event, a repeated reconnect produces no duplicate, and Word reconciliation reuses one idempotency key. FFmpeg regenerates the H.264/AAC video, eight timestamped proof frames, and `demo/video-proof/contact-sheet.png`.

The narrated wrapper is Windows-specific because it uses `System.Speech`. The Playwright proof logic in `demo/record-demo.mjs` is platform-neutral and can run against the same URLs where Chromium is installed.

## Package Contract

```powershell
pwsh ./scripts/package.ps1 --dry-run --json
pwsh ./scripts/package.ps1
```

POSIX: `sh ./scripts/package.sh --dry-run --json` or `sh ./scripts/package.sh`.

The tracked-file inventory is filtered through `scripts/delivery-manifest.json`, which names allowed top-level files/trees, internal exclusions, and approved SHA-256 hashes for every binary. Raw content plus extractable DOCX/PDF content is checked for credential and host-home patterns. The default output is outside the Git common checkout; an explicit output must be outside every listed worktree. Creation uses a temporary file, audits exact names/content, and atomically replaces the destination, so a failed audit preserves any prior archive.

## Stop Without Deleting Data

```powershell
docker compose stop
```

Restart with `pwsh ./scripts/demo.ps1`; PostgreSQL volume state and browser queues remain available.
