import type { QueueState } from "@fieldledger/offline-queue";
import { RefreshCw, ScanSearch } from "lucide-react";

import { ConflictPanel, type ConflictModel } from "../components/ConflictPanel";
import { QueueRow, type QueueRowModel } from "../components/QueueRow";
import { EmptyState } from "../components/States";

export interface QueuePageProps {
  commands: QueueRowModel[];
  conflict?: ConflictModel;
  pendingScope: string | null;
  onRetry: (commandId: string) => Promise<void>;
  onReconcile: (commandId: string) => Promise<void>;
  onRefreshReview: (commandId: string) => Promise<void>;
  onLater: () => void;
}

const blockers = new Set<QueueState>(["stale", "failed", "reconciliation_required"]);

export function QueuePage({ commands, conflict, pendingScope, onRetry, onReconcile, onRefreshReview, onLater }: QueuePageProps) {
  const blocker = commands.find((command) => blockers.has(command.state));
  return (
    <div className="page queue-page">
      <header className="queue-heading">
        <span className="eyebrow">{commands.length} ordered {commands.length === 1 ? "decision" : "decisions"}</span>
        <h1>{commands.length > 0 ? "Waiting to sync" : "Queue"}</h1>
        <p>Commands stay in phone sequence. Later decisions cannot pass a blocker.</p>
      </header>
      {commands.length === 0 ? <EmptyState title="All caught up" detail="There are no decisions waiting on this phone." /> : null}
      <div className="queue-list">
        {commands.map((command) => (
          <div key={command.id}>
            <QueueRow command={command} {...(blocker ? { blockedBy: blocker.sequence } : {})} />
            {command.state === "failed" ? (
              <button className="row-command" type="button" disabled={pendingScope === command.id} onClick={() => void onRetry(command.id)}>
                <RefreshCw aria-hidden="true" size={17} /> Retry failed command
              </button>
            ) : null}
            {command.state === "reconciliation_required" ? (
              <button className="row-command" type="button" disabled={pendingScope === command.id} onClick={() => void onReconcile(command.id)}>
                <ScanSearch aria-hidden="true" size={17} /> Reconcile outcome
              </button>
            ) : null}
          </div>
        ))}
      </div>
      {conflict ? <ConflictPanel conflict={conflict} busy={pendingScope === conflict.commandId} onRefreshReview={() => onRefreshReview(conflict.commandId)} onLater={onLater} /> : null}
    </div>
  );
}
