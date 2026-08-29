import { MetricBand } from "../components/MetricBand";
import { ReviewRow, type ReviewRowModel } from "../components/ReviewRow";
import { EmptyState, ErrorState } from "../components/States";

export interface InboxPageProps {
  metrics: { pending: number; queued: number; stale: number };
  cachedAt: string;
  batches: ReviewRowModel[];
  connection: "online" | "offline" | "error";
  pending: boolean;
  error?: string;
  onExportQueue?: () => Promise<void>;
  onResetQueue?: () => Promise<void>;
  onRefresh: () => Promise<void>;
}

function cachedTime(value: string): string {
  return new Intl.DateTimeFormat("en-US", { hour: "numeric", minute: "2-digit" }).format(new Date(value));
}

export function InboxPage({ metrics, cachedAt, batches, pending, error, onRefresh, onExportQueue, onResetQueue }: InboxPageProps) {
  return (
    <div className="page inbox-page">
      <MetricBand {...metrics} />
      <div className="section-heading">
        <div><span className="eyebrow">Assigned review</span><h1>Review inbox</h1></div>
        <span className="cached-time">Synced {cachedTime(cachedAt)}</span>
      </div>
      {error && onExportQueue && onResetQueue ? (
        <section className="error-state" role="alert">
          <FileWarning aria-hidden="true" />
          <h2>Saved queue cannot be read</h2>
          <p>{error} Export it before resetting; FieldLedger will not discard it silently.</p>
          <div className="panel-actions">
            <button className="button button--quiet" type="button" onClick={() => void onExportQueue()}>
              <Download aria-hidden="true" size={18} /> Export queue
            </button>
            <button className="button button--primary" type="button" onClick={() => void onResetQueue()}>
              <RotateCcw aria-hidden="true" size={18} /> Reset queue
            </button>
          </div>
        </section>
      ) : error ? <ErrorState title="Could not refresh" detail={error} onRetry={() => void onRefresh()} /> : null}
      {pending && batches.length === 0 ? <div className="loading-rows" role="status">Loading cached reviews…</div> : null}
      {!pending && batches.length === 0 ? <EmptyState title="No pending reviews" detail={`Last checked ${cachedTime(cachedAt)}. Refresh when new work is assigned.`} /> : null}
      {batches.length > 0 ? <div className="review-list">{batches.map((batch) => <ReviewRow batch={batch} key={batch.id} />)}</div> : null}
    </div>
  );
}
import { Download, FileWarning, RotateCcw } from "lucide-react";
