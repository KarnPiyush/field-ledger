import { AlertTriangle, CheckCircle2, Clock3, HelpCircle, RefreshCw } from "lucide-react";
import type { QueueCommand } from "@fieldledger/offline-queue";

import type { WordInstruction } from "../office/bridge";

function state(command: QueueCommand<WordInstruction>): {
  text: string;
  tone: string;
  icon: typeof Clock3;
} {
  switch (command.state) {
    case "pending":
    case "sending":
      return { text: "Waiting to sync", tone: "work", icon: Clock3 };
    case "applied":
      return { text: "Proposal ready", tone: "success", icon: CheckCircle2 };
    case "superseded":
      return { text: "Refreshed", tone: "success", icon: CheckCircle2 };
    case "stale":
      return { text: "Needs refresh", tone: "danger", icon: AlertTriangle };
    case "failed":
      return { text: "Nothing was applied", tone: "danger", icon: RefreshCw };
    case "reconciliation_required":
      return { text: "Needs reconciliation", tone: "info", icon: HelpCircle };
  }
}

interface QueueListProps {
  commands: readonly QueueCommand<WordInstruction>[];
  selectedId?: string | undefined;
  onSelect: (id: string) => void;
}

export function QueueList({ commands, selectedId, onSelect }: QueueListProps) {
  return (
    <section className="queue" aria-labelledby="queue-heading">
      <div className="section-heading">
        <h2 id="queue-heading">Queue</h2>
        <span>{commands.filter((command) => command.state !== "applied" && command.state !== "superseded").length} open</span>
      </div>
      {commands.length === 0 ? (
        <p className="empty-queue">No saved instructions</p>
      ) : (
        <ol className="queue-list">
          {commands.map((command) => {
            const status = state(command);
            const Icon = status.icon;
            return (
              <li key={command.id}>
                <button
                  type="button"
                  className={selectedId === command.id ? "queue-row is-selected" : "queue-row"}
                  onClick={() => onSelect(command.id)}
                  aria-label={`Instruction ${command.sequence}: ${command.payload.text}`}
                >
                  <span className="sequence">{command.sequence}</span>
                  <span className="queue-copy">
                    <strong>{command.payload.text}</strong>
                    <small>{command.payload.target.kind === "selection" ? "Selection" : "Document"}</small>
                  </span>
                  <span className={`queue-state queue-state--${status.tone}`}>
                    <Icon aria-hidden="true" /> {status.text}
                  </span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
