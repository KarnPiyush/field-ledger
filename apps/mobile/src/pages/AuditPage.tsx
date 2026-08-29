import { RadioTower, UserRound } from "lucide-react";

import { EmptyState } from "../components/States";
import { StatusBadge } from "../components/StatusBadge";

export interface AuditRowModel {
  id: string;
  eventType: string;
  actor: string;
  source: string;
  detail: string;
  correlationId: string;
  createdAt: string;
}

export interface AuditPageProps {
  rows: AuditRowModel[];
  pending: boolean;
  error?: string;
}

export function AuditPage({ rows, pending, error }: AuditPageProps) {
  return (
    <div className="page audit-page">
      <header className="page-heading"><span className="eyebrow">Authoritative history</span><h1>Audit trail</h1><p>Human decisions, relay source, provider lifecycle, and correlation references.</p></header>
      {pending ? <p role="status" className="loading-rows">Loading cached audit…</p> : null}
      {error ? <p role="alert" className="inline-error">{error}</p> : null}
      {!pending && rows.length === 0 ? <EmptyState title="No audit events" detail="Applied decisions and provider updates will appear here." /> : null}
      <div className="audit-list">
        {rows.map((row) => (
          <article className="audit-row" key={row.id}>
            <div className="audit-row__top"><StatusBadge label={row.eventType} tone="success" /><time dateTime={row.createdAt}>{new Date(row.createdAt).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</time></div>
            <h2>{row.detail}</h2>
            <p><UserRound aria-hidden="true" size={15} /> <strong>{row.actor}</strong></p>
            <p><RadioTower aria-hidden="true" size={15} /> {row.source}</p>
            <code>{row.correlationId}</code>
          </article>
        ))}
      </div>
    </div>
  );
}
