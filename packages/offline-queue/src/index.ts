export const workspace = "@fieldledger/offline-queue" as const;

export { drainInOrder, reconcileBlockedCommand } from "./drain.js";
export {
  canonicalDependencyJson,
  canonicalRequestJson,
  digestDependencyVector,
  digestRequest,
  makeIdempotencyKey,
} from "./fingerprint.js";
export { transitionCommand } from "./reducer.js";
export { decodeQueue, encodeQueue, QueueSerializationError } from "./serialization.js";
export type { DependencyFingerprintEntry, DigestAdapter } from "./fingerprint.js";
export type { PayloadValidator, QueueSerializationErrorCode } from "./serialization.js";
export type {
  ApplyResultDto,
  DecisionRequestDto,
  DependencyChangeDto,
  DependencyKindDto,
  DrainStatus,
  DrainSummary,
  NewCommand,
  QueueCommand,
  QueueProblem,
  QueueState,
  QueueStore,
  QueueTransition,
  Sender,
  SendResult,
} from "./types.js";
