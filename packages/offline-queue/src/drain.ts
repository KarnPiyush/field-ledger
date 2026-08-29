import { transitionCommand } from "./reducer.js";
import type {
  DrainSummary,
  QueueCommand,
  QueueProblem,
  QueueStore,
  SendResult,
  Sender,
} from "./types.js";

function compareCommands<T>(left: QueueCommand<T>, right: QueueCommand<T>): number {
  if (left.clientId < right.clientId) return -1;
  if (left.clientId > right.clientId) return 1;
  return left.sequence - right.sequence;
}

function conflictingKeys<T>(commands: readonly QueueCommand<T>[]): ReadonlySet<string> {
  const hashes = new Map<string, string>();
  const conflicts = new Set<string>();
  for (const command of commands) {
    const prior = hashes.get(command.idempotencyKey);
    if (prior !== undefined && prior !== command.payloadHash) conflicts.add(command.idempotencyKey);
    hashes.set(command.idempotencyKey, command.payloadHash);
  }
  return conflicts;
}

function conflictProblem(command: QueueCommand<unknown>): Extract<QueueProblem, { kind: "failed" }> {
  return {
    kind: "failed",
    code: "idempotency-conflict",
    title: "Conflicting queued command",
    detail: `Idempotency key ${command.idempotencyKey} has a different payload hash`,
  };
}

function stopped(
  status: Exclude<DrainSummary["status"], "drained">,
  applied: number,
  processed: number,
  commandId: string,
): DrainSummary {
  return { status, applied, processed, commandId };
}

export async function drainInOrder<T>(
  store: QueueStore<T>,
  sender: Sender<T>,
): Promise<DrainSummary> {
  const commands = [...(await store.list())].sort(compareCommands);
  const conflicts = conflictingKeys(commands);
  let applied = 0;
  let processed = 0;

  for (const listed of commands) {
    if (listed.state === "applied" || listed.state === "superseded") continue;
    if (
      listed.state === "stale" ||
      listed.state === "failed" ||
      listed.state === "reconciliation_required"
    ) {
      return stopped(listed.state, applied, processed, listed.id);
    }

    let sending = listed;
    if (listed.state === "pending") {
      sending = transitionCommand(listed, { kind: "begin_send" });
      if (!(await store.compareAndSet(listed.id, "pending", sending))) {
        return stopped("contended", applied, processed, listed.id);
      }
    }

    if (conflicts.has(sending.idempotencyKey)) {
      processed += 1;
      const failed = transitionCommand(sending, {
        kind: "failed",
        problem: conflictProblem(sending),
      });
      if (!(await store.compareAndSet(sending.id, "sending", failed))) {
        return stopped("contended", applied, processed, sending.id);
      }
      return stopped("failed", applied, processed, sending.id);
    }

    const result =
      listed.state === "sending"
        ? await sender.reconcile(sending)
        : await sender.send(sending);
    processed += 1;

    const next = transitionCommand(sending, result);
    if (!(await store.compareAndSet(sending.id, "sending", next))) {
      return stopped("contended", applied, processed, sending.id);
    }
    if (result.kind === "applied") {
      applied += 1;
      continue;
    }
    return stopped(result.kind, applied, processed, sending.id);
  }

  return { status: "drained", applied, processed };
}

export async function reconcileBlockedCommand<T>(
  store: QueueStore<T>,
  sender: Sender<T>,
  command: QueueCommand<T>,
): Promise<SendResult> {
  if (command.state !== "reconciliation_required") {
    throw new Error("Only reconciliation-required commands can be reconciled manually");
  }
  const sending = transitionCommand(command, { kind: "begin_send" });
  if (!(await store.compareAndSet(command.id, "reconciliation_required", sending))) {
    throw new Error("Command changed before reconciliation");
  }
  const result = await sender.reconcile(sending);
  const next = transitionCommand(sending, result);
  if (!(await store.compareAndSet(command.id, "sending", next))) {
    throw new Error("Command changed during reconciliation");
  }
  return result;
}
