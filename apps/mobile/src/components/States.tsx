import { AlertTriangle, Inbox, RotateCw } from "lucide-react";

export function EmptyState({ title, detail }: { title: string; detail: string }) {
  return <section className="empty-state"><Inbox aria-hidden="true" /><h2>{title}</h2><p>{detail}</p></section>;
}

export function ErrorState({ title, detail, onRetry }: { title: string; detail: string; onRetry?: () => void }) {
  return (
    <section className="error-state" role="alert">
      <AlertTriangle aria-hidden="true" />
      <h2>{title}</h2><p>{detail}</p>
      {onRetry ? <button className="button button--quiet" type="button" onClick={onRetry}><RotateCw aria-hidden="true" size={18} /> Retry</button> : null}
    </section>
  );
}
