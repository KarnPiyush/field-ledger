import type { QueueState } from "@fieldledger/offline-queue";

import { StatusBadge, type StatusTone } from "./StatusBadge";

export interface QueueRowModel {
  id: string;
  sequence: number;
  summary: string;
  detail: string;
  state: QueueState;
}

const label: Record<QueueState, string> = {
  pending: "Queued",
  sending: "Sending",
  applied: "Applied",
  superseded: "Refreshed",
  stale: "Needs refresh",
  failed: "Failed",
  reconciliation_required: "Reconcile",
};

export function QueueRow({ command, blockedBy }: { command: QueueRowModel; blockedBy?: number }) {
  const tone: StatusTone = command.state === "applied" || command.state === "superseded" ? "success" : command.state === "stale" || command.state === "failed" ? "danger" : command.state === "sending" ? "info" : "work";
  return (
    <article className="queue-row">
      <span className="sequence" aria-label={`Sequence ${command.sequence}`}>{command.sequence}</span>
      <div className="queue-row__content">
        <h2>{command.summary}</h2>
        <p>{blockedBy && command.sequence > blockedBy ? `Waits for item ${blockedBy}` : command.detail}</p>
      </div>
      <StatusBadge label={label[command.state]} tone={tone} />
    </article>
  );
}
