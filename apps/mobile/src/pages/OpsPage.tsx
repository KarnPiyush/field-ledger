import { RefreshCw, ScanSearch } from "lucide-react";

import { EmptyState } from "../components/States";
import { StatusBadge, type StatusTone } from "../components/StatusBadge";

export interface OpsJobModel {
  id: string;
  name: string;
  status: string;
  detail: string;
}

export interface OpsPageProps {
  jobs: OpsJobModel[];
  pendingScope: string | null;
  onPoll: (jobId: string) => Promise<void>;
  onReconcile: (jobId: string) => Promise<void>;
}

function tone(status: string): StatusTone {
  if (status === "Completed") return "success";
  if (status === "Failed" || status === "Cancelled") return "danger";
  if (status === "Review ready" || status === "Needs reconciliation") return "work";
  return "info";
}

export function OpsPage({ jobs, pendingScope, onPoll, onReconcile }: OpsPageProps) {
  const selected = jobs[0];
  return (
    <div className="page ops-page">
      <header className="page-heading"><span className="eyebrow">Provider operations</span><h1>Jobs and recovery</h1><p>Poll only active work. Reconcile an ambiguous response before reporting an outcome.</p></header>
      {jobs.length === 0 ? <EmptyState title="No provider jobs" detail="Processing and recovery jobs will appear here." /> : null}
      <div className="ops-layout">
        <div className="ops-list" role="list" aria-label="Provider jobs">
          {jobs.map((job) => (
            <article className="ops-row" role="listitem" key={job.id}>
              <div><h2>{job.name}</h2><p>{job.detail}</p></div>
              <StatusBadge label={job.status} tone={tone(job.status)} />
              {job.status === "Processing" ? (
                <button className="icon-button" type="button" title={`Poll ${job.name}`} aria-label={`Poll ${job.name}`} disabled={pendingScope === job.id} onClick={() => void onPoll(job.id)}><RefreshCw aria-hidden="true" /></button>
              ) : null}
              {job.status === "Needs reconciliation" ? (
                <button className="icon-button" type="button" title={`Reconcile ${job.name}`} aria-label={`Reconcile ${job.name}`} disabled={pendingScope === job.id} onClick={() => void onReconcile(job.id)}><ScanSearch aria-hidden="true" /></button>
              ) : null}
            </article>
          ))}
        </div>
        {selected ? (
          <aside className="ops-detail">
            <span className="eyebrow">Selected job</span>
            <h2>{selected.name}</h2>
            <dl><div><dt>Provider state</dt><dd>{selected.detail}</dd></div><div><dt>Job ID</dt><dd><code>{selected.id}</code></dd></div></dl>
          </aside>
        ) : null}
      </div>
    </div>
  );
}
