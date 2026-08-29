# Measurement

## Method

`pwsh ./scripts/measure-setup.ps1 --samples 7` produced the checked [workflow rows](../../demo/measurements/workflow-raw.csv), [workflow summary](../../demo/measurements/workflow-summary.json), and [setup rows](../../demo/measurements/setup-raw.csv). Every workflow sample parses the five-file synthetic pile, runs the real deterministic stage engine, resolves six facts/citations, checks rules, drafts artifacts, and records provider calls/cost.

Environment: Windows 11 Enterprise 10.0.26200; Intel Core Ultra 7 265U; 31.5 GB RAM; Docker 29.4.3; Compose 5.1.3; Node 24.15.0 executing a Node 22-compatible project contract; pnpm 11.7.0; uv 0.11.28; Python 3.12 managed by uv. Input size was 38,119 bytes.

## Results

| Measure | Result |
|---|---:|
| Samples | 7 |
| Workflow p50 / median | 20.477 ms |
| Workflow p95, nearest-rank `ceil(0.95*n)` | 26.645 ms |
| Workflow min / max | 17.414 / 26.645 ms |
| Workflow population standard deviation | 3.619 ms |
| Citation coverage | 100% in every sample |
| Published facts | 6 in every sample |
| Provider calls / measured cost | 0 / USD 0.000000 |
| Warm unchanged-block ratio | 100% |
| Cached Compose-config p50 / p95 | 295.565 / 333.925 ms |

The first workflow row is labeled cold because it has no in-process prior artifact cache. Rows two through seven reuse a prior deterministic result, so their unchanged blocks compare byte-for-byte. This timing loop does not execute a client queue, so it reports no queue outcome; queue behavior is verified separately by shared, phone, and Word tests.

## Setup Boundaries

All seven setup rows are `warm_cached_configuration`. They measure real Compose configuration resolution, not image build or pull. The host already contained Docker and package caches, and removing shared caches would have been destructive, so an uncached pull measurement is `not_measured`. The one-command start target remains documented, but this report does not invent a cold-start number.

## Limitations

These are local deterministic micro-benchmarks, not production capacity or end-to-end network latency. Sample count is small; Windows background activity adds variance; PDF/DOCX files are intentionally small; provider latency and cost are zero by design. Reported p95 is the maximum of seven samples under nearest-rank calculation. Re-run the command on the evaluation host before comparing machines.
