# FieldLedger

FieldLedger turns a construction document pile into a cited change register and executive brief, then lets a human review proposed document changes on a phone, including offline. Local drafts are not applied decisions: the server must confirm the outcome. The Word companion queues instructions and opens review; it does not write approved content back into Word automatically.

## Start Here

| Question | Read |
|---|---|
| What happens end to end? | [User walkthrough](docs/user-flow.md) |
| How do components fit together? | [One-page diagram](docs/architecture-one-page.md) |
| Where is each responsibility implemented? | [Architecture and code map](docs/architecture.md) |
| Which assignment parts have evidence? | [Requirement matrix](docs/submission/requirement-matrix.md) |
| What remains limited or unverified? | [Known limitations](docs/submission/limitations.md) |

The assigned Task 2 build is **Offline-capable review and approval client for a phone**. The common document-analysis task and optional Word companion are separate parts of this repository. The default demo uses deterministic analysis and a no-key provider; it is not a live SuperDocs acceptance run.

![FieldLedger cited phone review](docs/images/fieldledger-mobile-review.png)

## Quick Start

**Current checkout caveat:** the tracked contract PDF bytes disagree with their approved fixture hashes. This predates the documentation update and blocks canonical demo seeding and package verification. The commands below describe the intended deterministic run; see [the exact blocker](docs/submission/limitations.md) before attempting fresh acceptance.

Prerequisites: Windows 11 or a POSIX shell, Docker Desktop with Compose, Git, Python 3.12 through `uv`, Node 22, and pnpm 11.

```powershell
pwsh ./scripts/demo.ps1
```

No SuperDocs API key is required. The command uses the deterministic provider, builds six services, runs migrations, waits for readiness, and is safe to run again.

## Expected Result

- Phone review: `http://127.0.0.1:4173/inbox`
- Word browser demo: `http://127.0.0.1:4174/taskpane.html`
- API reference: `http://127.0.0.1:8000/docs`
- First review: **Riverside Medical Center / Change order 014 / 5 cited items**

## Demo Flow

Open the Riverside phone batch while online so its review data is cached. Inspect the signed change-order citation, then save mixed decisions for all five items while offline. Queue view shows one ordered five-decision command. Reconnect once and check the Applied row and one new `review.decisions_applied` audit event. See the [walkthrough](docs/user-flow.md), [recorded narration](demo/narration.txt), and [demo video](demo/fieldledger-superdocs-demo.mp4). These recordings document the original deterministic run; the narration's local-only delivery statement is historical.

## Architecture

PostgreSQL is the only workflow authority. Workers claim explicit stages with leases and fencing tokens, and commit a checkpoint, content-addressed artifacts, dependencies, audit, and outbox record together. REST and MCP call the same services. Both clients share a pure ordered queue and retain the original idempotency key through reconciliation.

Read the [developer architecture](docs/architecture.md), [one-page architecture](docs/architecture-one-page.md), and [layman user flow](docs/user-flow.md).

## Commands

| Purpose | Windows | POSIX |
|---|---|---|
| Start deterministic demo | `pwsh ./scripts/demo.ps1` | `sh ./scripts/demo.sh` |
| Diagnose setup | `pwsh ./scripts/doctor.ps1` | `sh ./scripts/doctor.sh` |
| Verify everything | `pwsh ./scripts/verify.ps1` | `sh ./scripts/verify.sh` |
| Record measurements | `pwsh ./scripts/measure-setup.ps1` | `sh ./scripts/measure-setup.sh` |
| Inspect MCP | `pwsh ./scripts/mcp-inspector.ps1` | `sh ./scripts/mcp-inspector.sh` |
| Reset synthetic state | `pwsh ./scripts/reset.ps1 --confirm RESET-SYNTHETIC-DATA` | `sh ./scripts/reset.sh --confirm RESET-SYNTHETIC-DATA` |
| Create local ZIP | `pwsh ./scripts/package.ps1` | `sh ./scripts/package.sh` |

The complete operational contract and recovery commands are in [operations](docs/operations.md). `verify --fast` skips browser suites and container image builds; complete verification remains the default.

## Tests

```powershell
uv run pytest apps/api/tests -q
pnpm test
pnpm --filter @fieldledger/mobile test:e2e
pnpm --filter @fieldledger/word test:e2e
pnpm api:check
pnpm build
docker compose config --quiet
```

Delivery contracts also resolve documentation links, parse Mermaid, validate fixture citations, recompute measurement summaries, probe representative video frames, scan for secret patterns, and audit the package allowlist. Test files describe executable checks, not proof that every check passed on your machine.

## Measurements

The checked run used seven deterministic samples. It records raw timings, p50, nearest-rank p95, input size, cache state, stage/provider cost, citation coverage, and unchanged-block ratio. See [measurement results](docs/submission/measurement.md) and [raw data](demo/measurements/workflow-raw.csv). Cold image-pull time is not claimed because the host already had caches.

## Provider Boundary

Deterministic mode is the default and makes no external provider call. The optional SuperDocs adapter is server-only and covered with fake transports. A live credential is never needed for the demo, tests, measurements, video, or archive. See [API reference](docs/api-reference.md) and [MCP boundary](docs/mcp.md).

## License And Credits

FieldLedger source is provided under the [MIT License](LICENSE). All included construction fixtures, screenshots, narration, and video are synthetic project assets; no stock or customer media is included. Third-party runtime and development packages retain their upstream licenses, with exact resolved versions recorded in `uv.lock` and `pnpm-lock.yaml`; dependency directories are not redistributed in the delivery ZIP.

## Troubleshooting

Run `pwsh ./scripts/doctor.ps1` first. Each failed check prints one recovery command. Common fixes are starting Docker Desktop, freeing the configured `POSTGRES_PORT`, `FIELDLEDGER_API_PORT`, `MOBILE_PORT`, and `WORD_PORT`, and running `docker compose logs api db`. Word host guidance is in [Word sideload](docs/word-sideload.md).

## Limitations

Native Word desktop sideload is not claimed; the Office bridge is adapter-tested and the browser task pane is visually tested. No live SuperDocs request was made. Cold image pulls, multi-host Office behavior, DMS integration, and automatic conflict overwrite are not claimed. Read the complete [limitations](docs/submission/limitations.md).

## Delivery Scope

Source and included artifacts are published in [KarnPiyush/field-ledger](https://github.com/KarnPiyush/field-ledger). The repository is private as of 14 September 2026; reviewers need access. Publication does not establish a hosted application, live provider acceptance, or submission-form/Drive delivery.

`scripts/delivery-manifest.json` limits packaging to tracked delivery files and pins approved binary hashes. The package command scans content, writes a temporary ZIP outside the checkout, and replaces the requested output only after its checks pass. See [operations](docs/operations.md) for packaging and verification commands.
