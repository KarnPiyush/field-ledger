import { canonicalRequestJson, makeIdempotencyKey } from "./fingerprint.js";
import type {
  DependencyChangeDto,
  DependencyKindDto,
  QueueCommand,
  QueueProblem,
  QueueState,
} from "./types.js";

export type QueueSerializationErrorCode = "corrupt" | "unsupported_version";
export type PayloadValidator<T> = (value: unknown) => value is T;

export class QueueSerializationError extends Error {
  constructor(
    readonly code: QueueSerializationErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "QueueSerializationError";
  }
}

const states = new Set<QueueState>([
  "pending",
  "sending",
  "applied",
  "superseded",
  "stale",
  "failed",
  "reconciliation_required",
]);

const dependencyKinds = new Set<DependencyKindDto>([
  "target_document_version",
  "source_version",
  "rule_set_version",
  "policy_version",
  "proposal",
  "workflow_artifact",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isString(value: unknown, allowEmpty = false): value is string {
  return typeof value === "string" && (allowEmpty || value.length > 0);
}

function isOptionalString(value: unknown): value is string | undefined {
  return value === undefined || typeof value === "string";
}

function isChange(value: unknown): value is DependencyChangeDto {
  return (
    isRecord(value) &&
    dependencyKinds.has(value.kind as DependencyKindDto) &&
    isString(value.key) &&
    (value.reviewed_hash === null || isString(value.reviewed_hash, true)) &&
    (value.current_hash === null || isString(value.current_hash, true))
  );
}

function isProblem(value: unknown): value is QueueProblem {
  if (
    !isRecord(value) ||
    !isString(value.code) ||
    !isString(value.title) ||
    !isString(value.detail) ||
    !isOptionalString(value.correlationId)
  ) {
    return false;
  }
  if (value.kind === "stale") {
    return Array.isArray(value.changes) && value.changes.every(isChange);
  }
  return (
    value.kind === "offline" ||
    value.kind === "failed" ||
    value.kind === "reconciliation_required"
  );
}

function hasValidStateInvariant(
  state: QueueState,
  attempts: number,
  lastProblem: QueueProblem | undefined,
): boolean {
  if (state === "pending") return lastProblem === undefined || lastProblem.kind === "offline";
  if (state === "sending" || state === "applied" || state === "superseded") {
    return attempts > 0 && lastProblem === undefined;
  }
  return attempts > 0 && lastProblem?.kind === state;
}

function validateCommand<T>(value: unknown): asserts value is QueueCommand<T> {
  if (!isRecord(value)) throw new QueueSerializationError("corrupt", "Invalid queue command");
  const state = value.state;
  const sequence = value.sequence;
  const attempts = value.attempts;
  const lastProblem = value.lastProblem;
  const validProblem = lastProblem === undefined || isProblem(lastProblem);
  let validPayload = true;
  try {
    canonicalRequestJson(value.payload);
  } catch {
    validPayload = false;
  }
  if (
    value.schemaVersion !== 1 ||
    !isString(value.id) ||
    !isString(value.clientId) ||
    !isString(value.projectId) ||
    !Number.isSafeInteger(sequence) ||
    (sequence as number) <= 0 ||
    !isString(value.idempotencyKey) ||
    value.idempotencyKey !== makeIdempotencyKey(value.clientId as string, sequence as number) ||
    !isString(value.reviewedDigest) ||
    !isString(value.payloadHash) ||
    !validPayload ||
    !states.has(state as QueueState) ||
    !isString(value.createdAt) ||
    !Number.isSafeInteger(attempts) ||
    (attempts as number) < 0 ||
    !isOptionalString(value.deduplicationKey) ||
    !validProblem ||
    !hasValidStateInvariant(
      state as QueueState,
      attempts as number,
      lastProblem as QueueProblem | undefined,
    )
  ) {
    throw new QueueSerializationError("corrupt", "Invalid queue command");
  }
}

export function encodeQueue<T>(commands: readonly QueueCommand<T>[]): string {
  for (const command of commands) validateCommand<T>(command);
  return JSON.stringify({ schemaVersion: 1, commands });
}

export function decodeQueue(serialized: string): readonly QueueCommand<unknown>[];
export function decodeQueue<T>(
  serialized: string,
  validatePayload: PayloadValidator<T>,
): readonly QueueCommand<T>[];
export function decodeQueue<T>(
  serialized: string,
  validatePayload?: PayloadValidator<T>,
): readonly QueueCommand<T>[] {
  let envelope: unknown;
  try {
    envelope = JSON.parse(serialized);
  } catch {
    throw new QueueSerializationError("corrupt", "Queue data is not valid JSON");
  }
  if (!isRecord(envelope)) {
    throw new QueueSerializationError("corrupt", "Queue envelope is invalid");
  }
  if (envelope.schemaVersion !== 1) {
    if (envelope.schemaVersion !== undefined) {
      throw new QueueSerializationError("unsupported_version", "Unsupported queue version");
    }
    throw new QueueSerializationError("corrupt", "Queue version is missing");
  }
  if (!Array.isArray(envelope.commands)) {
    throw new QueueSerializationError("corrupt", "Queue commands are invalid");
  }
  for (const command of envelope.commands) {
    validateCommand<T>(command);
    if (validatePayload !== undefined && !validatePayload(command.payload)) {
      throw new QueueSerializationError("corrupt", "Queue payload is invalid");
    }
  }
  return envelope.commands as QueueCommand<T>[];
}
