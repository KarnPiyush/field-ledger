import { RotateCcw, X } from "lucide-react";

export interface ConflictChange {
  key: string;
  kind: string;
  reviewed: string;
  current: string;
  delta: string;
}

export interface ConflictModel {
  commandId: string;
  title: string;
  detail: string;
  changes: ConflictChange[];
}

interface ConflictPanelProps {
  conflict: ConflictModel;
  busy: boolean;
  onRefreshReview: () => Promise<void>;
  onLater: () => void;
}

export function ConflictPanel({ conflict, busy, onRefreshReview, onLater }: ConflictPanelProps) {
  return (
    <section className="conflict-panel" aria-labelledby="conflict-title">
      <div className="conflict-panel__heading">
        <h2 id="conflict-title">{conflict.title}</h2>
        <p>{conflict.detail}</p>
      </div>
      <div className="conflict-panel__changes">
        {conflict.changes.map((change) => (
          <article key={`${change.kind}:${change.key}`}>
            <h3>{change.key}</h3>
            <dl>
              <div><dt>Reviewed</dt><dd>{change.reviewed}</dd></div>
              <div><dt>Current</dt><dd>{change.current}</dd></div>
            </dl>
            <p>{change.delta}</p>
          </article>
        ))}
      </div>
      <div className="conflict-panel__actions">
        <button className="button button--primary" type="button" disabled={busy} onClick={() => void onRefreshReview()}>
          <RotateCcw aria-hidden="true" size={18} /> Refresh for review
        </button>
        <button className="button button--quiet" type="button" disabled={busy} onClick={onLater}>
          <X aria-hidden="true" size={18} /> Later
        </button>
      </div>
    </section>
  );
}
