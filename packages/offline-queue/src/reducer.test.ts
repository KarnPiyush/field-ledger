import { describe, expect, it } from "vitest";

import {
  transitionCommand,
  type DecisionRequestDto,
  type QueueCommand,
  type QueueProblem,
  type QueueState,
  type QueueTransition,
} from "./index.js";

const payload: DecisionRequestDto = {
  confirmation_token: "confirmation",
  decisions: [{ item_id: "item-1", decision: "reject", note: "Needs detail" }],
};

const changes = [
  {
    kind: "source_version" as const,
    key: "source:1",
    reviewed_hash: "sha256:old",
    current_hash: "sha256:new",
  },
];

const problems = {
  offline: {
    kind: "offline",
    code: "offline",
    title: "Offline",
    detail: "No bytes were sent",
    correlationId: "corr-offline",
  },
  stale: {
    kind: "stale",
    code: "dependency-stale",
    title: "Review is stale",
    detail: "A dependency changed",
    correlationId: "corr-stale",
    changes,
  },
  failed: {
    kind: "failed",
    code: "rejected",
    title: "Could not apply",
    detail: "The server rejected the command",
    correlationId: "corr-failed",
  },
  reconciliation: {
    kind: "reconciliation_required",
    code: "response-lost",
    title: "Check submission",
    detail: "The response was lost after dispatch",
    correlationId: "corr-reconcile",
  },
} satisfies Record<string, QueueProblem>;

function command(state: QueueState, lastProblem?: QueueProblem): QueueCommand<DecisionRequestDto> {
  const base: QueueCommand<DecisionRequestDto> = {
    schemaVersion: 1,
    id: "command-1",
    clientId: "client-a",
    projectId: "project-a",
    sequence: 1,
    idempotencyKey: "client-a:1",
    reviewedDigest: "sha256:reviewed",
    payloadHash: "sha256:payload",
    payload,
    state,
    createdAt: "2026-11-01T01:30:00-04:00",
    attempts: state === "pending" ? 0 : 1,
  };
  return lastProblem === undefined ? base : { ...base, lastProblem };
}

describe("transitionCommand", () => {
  it("moves pending to sending, increments attempts, and clears an old offline problem", () => {
    const original = command("pending", problems.offline);

    const next = transitionCommand(original, { kind: "begin_send" });

    expect(next).toEqual({ ...original, state: "sending", attempts: 1, lastProblem: undefined });
    expect(original.state).toBe("pending");
  });

  it.each([
    [{ kind: "applied" }, "applied", undefined],
    [{ kind: "offline", problem: problems.offline }, "pending", problems.offline],
    [{ kind: "stale", problem: problems.stale }, "stale", problems.stale],
    [{ kind: "failed", problem: problems.failed }, "failed", problems.failed],
    [
      { kind: "reconciliation_required", problem: problems.reconciliation },
      "reconciliation_required",
      problems.reconciliation,
    ],
  ] as const)("applies the sending transition %#", (transition, state, lastProblem) => {
    const original = command("sending");

    const next = transitionCommand(original, transition);

    expect(next.state).toBe(state);
    expect(next.lastProblem).toBe(lastProblem);
    expect(next.payload).toBe(payload);
  });

  it("allows only an explicit user retry from failed", () => {
    const original = command("failed", problems.failed);

    expect(transitionCommand(original, { kind: "retry_failed" })).toEqual({
      ...original,
      state: "pending",
      lastProblem: undefined,
    });
  });

  it("supersedes stale history after an authoritative replacement is cached", () => {
    const original = command("stale", problems.stale);

    expect(transitionCommand(original, { kind: "supersede_stale" })).toEqual({
      ...original,
      state: "superseded",
      lastProblem: undefined,
    });
  });

  it.each([
    ["pending", { kind: "applied" }],
    ["sending", { kind: "begin_send" }],
    ["applied", { kind: "begin_send" }],
    ["stale", { kind: "retry_failed" }],
    ["failed", { kind: "failed", problem: problems.failed }],
    ["reconciliation_required", { kind: "retry_failed" }],
  ] satisfies ReadonlyArray<readonly [QueueState, QueueTransition]>) (
    "rejects illegal transition from %s",
    (state, transition) => {
      const original = command(
        state,
        state === "stale"
          ? problems.stale
          : state === "failed"
            ? problems.failed
            : state === "reconciliation_required"
              ? problems.reconciliation
              : undefined,
      );

      expect(() => transitionCommand(original, transition)).toThrow(
        `Illegal queue transition ${state} -> ${transition.kind}`,
      );
      expect(original.state).toBe(state);
    },
  );
});
