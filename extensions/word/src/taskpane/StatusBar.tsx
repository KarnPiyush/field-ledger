import { CircleAlert, Cloud, CloudOff, LoaderCircle } from "lucide-react";

interface StatusBarProps {
  online: boolean;
  busy: boolean;
  message?: string | undefined;
  error?: boolean;
}

export function StatusBar({ online, busy, message, error }: StatusBarProps) {
  const Icon = error ? CircleAlert : busy ? LoaderCircle : online ? Cloud : CloudOff;
  const text = message ?? (busy ? "Checking queue" : online ? "Online" : "Offline");
  return (
    <div
      className={`connection-strip${error ? " connection-strip--error" : online ? " connection-strip--online" : ""}`}
      role="status"
      aria-live="polite"
    >
      <Icon className={busy ? "is-spinning" : undefined} aria-hidden="true" />
      <span>{text}</span>
    </div>
  );
}
