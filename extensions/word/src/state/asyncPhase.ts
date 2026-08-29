export type AsyncOutcome =
  | "processing"
  | "continue_prompt"
  | "awaiting_approval"
  | "review_ready"
  | "preview_ready"
  | "PreviewReady"
  | "completed"
  | "applied"
  | "stale"
  | "failed"
  | "cancelled"
  | "timeout"
  | "offline"
  | "fetch_error"
  | "stream_error"
  | "post_refresh_error"
  | "storage_error"
  | "reconciliation_required"
  | "lost_response";

export type PhaseStatus =
  | "idle"
  | "pending"
  | "processing"
  | "success"
  | "review"
  | "attention"
  | "error"
  | "reconcile";

export interface AsyncPhase {
  status: PhaseStatus;
  busy: boolean;
  message?: string;
  correlationId?: string;
}

export type AsyncPhases = Readonly<Record<string, AsyncPhase>>;

export type AsyncPhaseAction =
  | { type: "begin"; scope: string }
  | {
      type: "finish";
      scope: string;
      outcome: AsyncOutcome;
      message?: string;
      correlationId?: string;
    }
  | { type: "clear"; scope: string };

const IDLE: AsyncPhase = { status: "idle", busy: false };

export function createAsyncPhases(): AsyncPhases {
  return {};
}

export function phaseFor(phases: AsyncPhases, scope: string): AsyncPhase {
  return phases[scope] ?? IDLE;
}

function terminalStatus(outcome: AsyncOutcome): PhaseStatus {
  switch (outcome) {
    case "processing":
    case "continue_prompt":
      return "processing";
    case "awaiting_approval":
    case "review_ready":
    case "preview_ready":
    case "PreviewReady":
      return "review";
    case "completed":
    case "applied":
      return "success";
    case "stale":
      return "attention";
    case "reconciliation_required":
    case "lost_response":
      return "reconcile";
    case "failed":
    case "cancelled":
    case "timeout":
    case "offline":
    case "fetch_error":
    case "stream_error":
    case "post_refresh_error":
    case "storage_error":
      return "error";
  }
}

export function asyncPhaseReducer(
  phases: AsyncPhases,
  action: AsyncPhaseAction,
): AsyncPhases {
  if (action.type === "clear") {
    if (!(action.scope in phases)) return phases;
    const next = { ...phases };
    delete next[action.scope];
    return next;
  }
  if (action.type === "begin") {
    return { ...phases, [action.scope]: { status: "pending", busy: true } };
  }
  const status = terminalStatus(action.outcome);
  return {
    ...phases,
    [action.scope]: {
      status,
      busy: status === "processing",
      ...(action.message === undefined ? {} : { message: action.message }),
      ...(action.correlationId === undefined ? {} : { correlationId: action.correlationId }),
    },
  };
}
