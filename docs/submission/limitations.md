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

The project is local-only: no Git remote, remote branch, hosted app, public video, or cloud archive. The final artifact is an audited ZIP created outside the repository after consolidated review.
