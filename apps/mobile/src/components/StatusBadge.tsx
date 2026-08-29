import {
  AlertTriangle,
  CheckCircle2,
  CircleDashed,
  Clock3,
  RefreshCw,
  XCircle,
} from "lucide-react";

export type StatusTone = "work" | "success" | "danger" | "info" | "muted";

export interface StatusBadgeProps {
  label: string;
  tone?: StatusTone;
}

export function StatusBadge({ label, tone = "muted" }: StatusBadgeProps) {
  const Icon = tone === "success"
    ? CheckCircle2
    : tone === "danger"
      ? XCircle
      : tone === "work"
        ? Clock3
        : tone === "info"
          ? RefreshCw
          : label.toLowerCase().includes("reconcil")
            ? AlertTriangle
            : CircleDashed;
  return (
    <span className={`status-badge status-badge--${tone}`}>
      <Icon aria-hidden="true" size={14} strokeWidth={2} />
      {label}
    </span>
  );
}
