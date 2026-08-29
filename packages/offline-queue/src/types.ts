import type { components } from "@fieldledger/api-client";

export type DecisionRequestDto = components["schemas"]["DecisionRequest"];
export type ApplyResultDto = components["schemas"]["ApplyResult"];
export type DependencyChangeDto = components["schemas"]["DependencyChange"];
export type DependencyKindDto = components["schemas"]["DependencyKind"];

export type QueueState =
  | "pending"
  | "sending"
  | "applied"
  | "superseded"
  | "stale"
  | "failed"
  | "reconciliation_required";

interface ProblemBase {
  code: string;
  title: string;
  detail: string;
  correlationId?: string;
}

export type QueueProblem =
  | (ProblemBase & { kind: "offline" })
  | (ProblemBase & { kind: "failed" })
  | (ProblemBase & { kind: "reconciliation_required" })
  | (ProblemBase & {
      kind: "stale";
      changes: readonly DependencyChangeDto[];
    });

export interface QueueCommand<T> {
  schemaVersion: 1;
  id: string;
  clientId: string;
  projectId: string;
  sequence: number;
  idempotencyKey: string;
  reviewedDigest: string;
  payloadHash: string;
  payload: T;
  state: QueueState;
  createdAt: string;
  attempts: number;
  deduplicationKey?: string;
  lastProblem?: QueueProblem;
}

export interface NewCommand<T> {
  id: string;
  clientId: string;
  projectId: string;
  deduplicationKey: string;
  reviewedDigest: string;
  payloadHash: string;
  payload: T;
  createdAt: string;
}

export interface QueueStore<T> {
  list(): Promise<readonly QueueCommand<T>[]>;
  allocateAndAppend(input: NewCommand<T>): Promise<QueueCommand<T>>;
  compareAndSet(
    id: string,
    expected: QueueState,
    next: QueueCommand<T>,
  ): Promise<boolean>;
}

export type QueueTransition =
  | { kind: "begin_send" }
  | { kind: "applied" }
  | { kind: "offline"; problem: Extract<QueueProblem, { kind: "offline" }> }
  | { kind: "stale"; problem: Extract<QueueProblem, { kind: "stale" }> }
  | { kind: "failed"; problem: Extract<QueueProblem, { kind: "failed" }> }
  | {
      kind: "reconciliation_required";
      problem: Extract<QueueProblem, { kind: "reconciliation_required" }>;
    }
  | { kind: "retry_failed" }
  | { kind: "supersede_stale" };

export type SendResult =
  | { kind: "applied"; response: ApplyResultDto }
  | { kind: "offline"; problem: Extract<QueueProblem, { kind: "offline" }> }
  | { kind: "stale"; problem: Extract<QueueProblem, { kind: "stale" }> }
  | { kind: "failed"; problem: Extract<QueueProblem, { kind: "failed" }> }
  | {
      kind: "reconciliation_required";
      problem: Extract<QueueProblem, { kind: "reconciliation_required" }>;
    };

export interface Sender<T> {
  send(command: QueueCommand<T>): Promise<SendResult>;
  reconcile(command: QueueCommand<T>): Promise<SendResult>;
}

export type DrainStatus =
  | "drained"
  | "offline"
  | "stale"
  | "failed"
  | "reconciliation_required"
  | "contended";

export type DrainSummary =
  | { status: "drained"; applied: number; processed: number }
  | {
      status: Exclude<DrainStatus, "drained">;
      applied: number;
      processed: number;
      commandId: string;
    };
