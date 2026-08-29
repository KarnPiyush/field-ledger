# Word add-in sideload

The FieldLedger Word add-in stores its queue in `Office.context.document.settings`.
It reads document and selection text to create a fingerprint, but it never writes Word
content. Every instruction is sent as a proposal and requires human review.

## Browser demo

```powershell
pnpm --filter @fieldledger/word dev -- --port 4177
```

Open `http://127.0.0.1:4177/taskpane.html`. The header must say `Browser demo`.
This adapter uses browser local storage and does not verify a native Word host.

The complete local stack serves the same browser adapter at
`http://127.0.0.1:4174/taskpane.html` after `pwsh ./scripts/demo.ps1`.

## Native configuration

Set the public handoff URL for the add-in and the server-only proxy values in the Vite
dev/preview process:

```text
VITE_FIELDLEDGER_REVIEW_URL=http://127.0.0.1:4173
FIELDLEDGER_API_TARGET=http://127.0.0.1:8000
FIELDLEDGER_DEMO_BOOTSTRAP_SECRET=<local server bootstrap secret>
```

`FIELDLEDGER_API_TARGET` and `FIELDLEDGER_DEMO_BOOTSTRAP_SECRET` are read by the Vite
server and are not exposed through `import.meta.env`. The browser requests the configured
`reviewer` session through the same-origin proxy, then obtains document and workflow IDs
from the authenticated idempotent demo seed. Set values in the process environment or an
ignored local environment file. Do not put bootstrap secrets, session tokens, or provider
credentials in the manifest, source, or tracked examples.

The add-in uses the generated FieldLedger client and preserves the queue's idempotency
key for both first submission and reconciliation.

## Windows desktop Word

1. Serve the production build over trusted HTTPS at `https://localhost:44317` and make sure the
   manifest URLs match that origin.
2. In Word, open **File > Options > Trust Center > Trust Center Settings > Trusted
   Add-in Catalogs**.
3. Add a trusted network share containing `extensions/word/manifest.xml`, restart Word,
   then choose **Home > Add-ins > Shared Folder > FieldLedger offline instructions**.
4. Confirm the header says `Microsoft Word`. Queue an instruction offline, close and
   reopen the pane, reconnect, and verify `Open proposal review` appears without the
   document body changing.

The manifest was structurally validated and the Office bridge was exercised with an
Office-compatible adapter. No desktop sideload claim is made without a named native host,
version, and completed persistence/replay check.

Office on the web and Word for macOS require their platform-specific catalog/deployment
flow. They are not considered verified unless named in the task report with an exact
host/version and a completed persistence/replay check.

## Recovery

- A queue save is successful only after Office `saveAsync` returns `succeeded`.
- Corrupt or unknown queue data blocks replay. Use **Export queue** before **Reset queue**.
- `Needs refresh` means the document or selection fingerprint moved; **Refresh
  instruction** creates a new queued command. There is no force or overwrite action.
- `Needs reconciliation` keeps the original idempotency key. Use **Reconcile** only after
  connectivity returns.
- A save timeout or `saveAsync` failure leaves the composer text available and clears the
  busy state. A fingerprint read failure leaves the command queued and shows a retryable
  error.
