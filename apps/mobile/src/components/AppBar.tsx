import { ArrowLeft, RefreshCw } from "lucide-react";

interface AppBarProps {
  title?: string;
  subtitle?: string;
  progress?: string;
  refreshing?: boolean;
  onRefresh?: () => void;
  onBack?: () => void;
}

export function AppBar({ title, subtitle, progress, refreshing, onRefresh, onBack }: AppBarProps) {
  return (
    <header className="app-bar">
      {onBack ? (
        <button className="icon-button" type="button" aria-label="Back to inbox" title="Back to inbox" onClick={onBack}>
          <ArrowLeft aria-hidden="true" />
        </button>
      ) : <span className="brand-mark" aria-hidden="true" />}
      <div className="app-bar__identity">
        {subtitle ? <span>{subtitle}</span> : null}
        {title ? <strong>{title}</strong> : <span className="wordmark">FieldLedger</span>}
      </div>
      {progress ? <span className="app-bar__progress">{progress}</span> : null}
      {onRefresh ? (
        <button className="icon-button app-bar__action" type="button" aria-label="Sync review batches" title="Sync review batches" disabled={refreshing} onClick={onRefresh}>
          <RefreshCw aria-hidden="true" className={refreshing ? "is-spinning" : undefined} />
        </button>
      ) : null}
    </header>
  );
}
