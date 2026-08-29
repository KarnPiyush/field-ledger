import { Cloud, CloudOff, TriangleAlert } from "lucide-react";

export interface ConnectionStripProps {
  state: "online" | "offline" | "error" | "syncing";
  queued: number;
  message?: string;
}

export function ConnectionStrip({ state, queued, message }: ConnectionStripProps) {
  const Icon = state === "offline" ? CloudOff : state === "error" ? TriangleAlert : Cloud;
  const text = message ?? (state === "offline"
    ? `Offline · ${queued} ${queued === 1 ? "decision" : "decisions"} saved on this phone`
    : state === "syncing"
      ? `Syncing ${queued} queued ${queued === 1 ? "decision" : "decisions"}`
      : queued > 0
        ? `Online · ${queued} waiting to sync`
        : "Online · Queue is caught up");
  return (
    <div className={`connection-strip connection-strip--${state}`} role="status" aria-live="polite">
      <Icon aria-hidden="true" size={16} />
      <span>{text}</span>
    </div>
  );
}
