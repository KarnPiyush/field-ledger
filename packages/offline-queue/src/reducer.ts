import type { QueueCommand, QueueState, QueueTransition } from "./types.js";

const legalTransitions: Record<QueueState, readonly QueueTransition["kind"][]> = {
  pending: ["begin_send"],
  sending: ["applied", "offline", "stale", "failed", "reconciliation_required"],
  applied: [],
  superseded: [],
  stale: ["supersede_stale"],
  failed: ["retry_failed"],
  reconciliation_required: ["begin_send"],
};

function withoutProblem<T>(command: QueueCommand<T>): QueueCommand<T> {
  const { lastProblem: _lastProblem, ...next } = command;
  return next;
}

export function transitionCommand<T>(
  command: QueueCommand<T>,
  transition: QueueTransition,
): QueueCommand<T> {
  if (!legalTransitions[command.state].includes(transition.kind)) {
    throw new Error(`Illegal queue transition ${command.state} -> ${transition.kind}`);
  }

  switch (transition.kind) {
    case "begin_send":
      return {
        ...withoutProblem(command),
        state: "sending",
        attempts: command.attempts + 1,
      };
    case "applied":
      return { ...withoutProblem(command), state: "applied" };
    case "offline":
      return { ...command, state: "pending", lastProblem: transition.problem };
    case "stale":
      return { ...command, state: "stale", lastProblem: transition.problem };
    case "failed":
      return { ...command, state: "failed", lastProblem: transition.problem };
    case "reconciliation_required":
      return {
        ...command,
        state: "reconciliation_required",
        lastProblem: transition.problem,
      };
    case "retry_failed":
      return { ...withoutProblem(command), state: "pending" };
    case "supersede_stale":
      return { ...withoutProblem(command), state: "superseded" };
  }
}
