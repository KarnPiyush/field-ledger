import { AlertTriangle, Clock3, RefreshCw } from "lucide-react";
import type { QueueCommand } from "@fieldledger/offline-queue";

import type { DocumentFingerprint, WordInstruction } from "../office/bridge";

interface ConflictPanelProps {
  command: QueueCommand<WordInstruction>;
  current?: DocumentFingerprint | undefined;
  busy: boolean;
  onRefresh: () => void;
  onLater: () => void;
  onRetry: () => void;
  onReconcile: () => void;
}

export function ConflictPanel({
  command,
  current,
  busy,
  onRefresh,
  onLater,
  onRetry,
  onReconcile,
}: ConflictPanelProps) {
  if (command.state === "stale") {
    return (
      <section className="attention-panel" aria-labelledby="attention-heading">
        <div className="attention-heading">
          <AlertTriangle aria-hidden="true" />
          <div>
            <h2 id="attention-heading">Needs refresh</h2>
            <p>Nothing was applied</p>
          </div>
        </div>
        <div className="preview-delta">
          <div>
            <span>Reviewed</span>
            <p>{command.payload.target.preview || "Current document"}</p>
            <code>{command.payload.base.contentDigest.slice(0, 12)}</code>
          </div>
          <div>
            <span>Current</span>
            <p>{current?.selectionPreview || "Document content changed"}</p>
            <code>{current?.contentDigest.slice(0, 12) ?? "Unavailable"}</code>
          </div>
        </div>
        <div className="panel-actions">
          <button type="button" className="primary-command" onClick={onRefresh} disabled={busy}>
            <RefreshCw aria-hidden="true" /> Refresh instruction
          </button>
          <button type="button" className="secondary-command" onClick={onLater} disabled={busy}>
            <Clock3 aria-hidden="true" /> Later
          </button>
        </div>
      </section>
    );
  }

  if (command.state === "reconciliation_required") {
    return (
      <section className="attention-panel attention-panel--info" aria-labelledby="attention-heading">
        <div className="attention-heading">
          <AlertTriangle aria-hidden="true" />
          <div>
            <h2 id="attention-heading">Needs reconciliation</h2>
            <p>{command.lastProblem?.detail}</p>
          </div>
        </div>
        <div className="panel-actions panel-actions--single">
          <button type="button" className="primary-command" onClick={onReconcile} disabled={busy}>
            <RefreshCw aria-hidden="true" /> Reconcile
          </button>
        </div>
      </section>
    );
  }

  if (command.state === "failed") {
    return (
      <section className="attention-panel" aria-labelledby="attention-heading">
        <div className="attention-heading">
          <AlertTriangle aria-hidden="true" />
          <div>
            <h2 id="attention-heading">Nothing was applied</h2>
            <p>{command.lastProblem?.detail}</p>
          </div>
        </div>
        <div className="panel-actions panel-actions--single">
          <button type="button" className="primary-command" onClick={onRetry} disabled={busy}>
            <RefreshCw aria-hidden="true" /> Retry
          </button>
        </div>
      </section>
    );
  }

  return null;
}
