import { describe, expect, it } from "vitest";

import {
  decodeQueue,
  encodeQueue,
  QueueSerializationError,
  type DecisionRequestDto,
  type QueueCommand,
  type QueueProblem,
  type QueueState,
} from "./index.js";

const payload: DecisionRequestDto = {
  confirmation_token: "confirmation-token",
  decisions: [{ item_id: "item-1", decision: "reject", note: "Preserve this note" }],
};

function isDecisionRequest(value: unknown): value is DecisionRequestDto {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const candidate = value as Partial<DecisionRequestDto>;
  return (
    typeof candidate.confirmation_token === "string" &&
    Array.isArray(candidate.decisions) &&
    candidate.decisions.every(
      (decision) =>
        decision !== null &&
        typeof decision === "object" &&
        typeof decision.item_id === "string" &&
        (decision.decision === "approve" || decision.decision === "reject") &&
        (decision.note === undefined || decision.note === null || typeof decision.note === "string"),
    )
  );
}

const problems = {
  offline: {
    kind: "offline",
    code: "offline",
    title: "Offline",
    detail: "Definitely not sent",
  },
  stale: {
    kind: "stale",
    code: "dependency-stale",
    title: "Stale",
    detail: "Dependencies changed",
    correlationId: "corr-stale",
    changes: [
      {
        kind: "policy_version" as const,
        key: "policy:1",
        reviewed_hash: "sha256:before",
        current_hash: null,
      },
      {
        kind: "source_version" as const,
        key: "source:2",
        reviewed_hash: null,
        current_hash: "sha256:after",
      },
      {
        kind: "workflow_artifact" as const,
        key: "artifact:manifest",
        reviewed_hash: "sha256:manifest-before",
        current_hash: "sha256:manifest-after",
      },
    ],
  },
  failed: {
    kind: "failed",
    code: "bad-request",
    title: "Submission failed",
    detail: "Fix the decisions",
    correlationId: "corr-failed",
  },
  reconciliation: {
    kind: "reconciliation_required",
    code: "timeout-after-dispatch",
    title: "Check submission",
    detail: "The final response is unknown",
    correlationId: "corr-reconcile",
  },
} satisfies Record<string, QueueProblem>;

function command(
  state: QueueState,
  lastProblem?: QueueProblem,
  sequence = 1,
): QueueCommand<DecisionRequestDto> {
  const value: QueueCommand<DecisionRequestDto> = {
    schemaVersion: 1,
    id: `command-${sequence}`,
    clientId: "client-a",
    projectId: "project-a",
    sequence,
    idempotencyKey: `client-a:${sequence}`,
    reviewedDigest: "sha256:reviewed",
    payloadHash: "sha256:payload",
    payload,
    state,
    createdAt: "2026-01-01T00:00:00Z",
    attempts: state === "pending" ? 0 : 1,
  };
  return lastProblem === undefined ? value : { ...value, lastProblem };
}

function expectEncodeRejected(value: unknown): void {
  expect(() => encodeQueue([value as QueueCommand<unknown>])).toThrowError(
    QueueSerializationError,
  );
}

describe("queue serialization", () => {
  it("round-trips every state without losing a decision note or exact stale changes", () => {
    const commands = [
      command("pending"),
      command("pending", problems.offline, 2),
      command("sending", undefined, 3),
      command("applied", undefined, 4),
      command("superseded", undefined, 8),
      command("stale", problems.stale, 5),
      command("failed", problems.failed, 6),
      command("reconciliation_required", problems.reconciliation, 7),
    ];

    const decoded = decodeQueue<DecisionRequestDto>(encodeQueue(commands), isDecisionRequest);

    expect(decoded).toEqual(commands);
    expect(decoded[0]?.payload.decisions[0]?.note).toBe("Preserve this note");
    expect(decoded[5]?.lastProblem).toEqual(problems.stale);
  });

  it("rejects a canonical but invalid adapter payload before replay", () => {
    const serialized = JSON.stringify({
      schemaVersion: 1,
      commands: [{ ...command("pending"), payload: {} }],
    });

    expect(() => decodeQueue(serialized, isDecisionRequest)).toThrowError(
      new QueueSerializationError("corrupt", "Queue payload is invalid"),
    );
  });

  it.each([
    ["not json", "corrupt"],
    ["null", "corrupt"],
    ['{"schemaVersion":2,"commands":[]}', "unsupported_version"],
    ['{"schemaVersion":1}', "corrupt"],
    ['{"commands":[]}', "corrupt"],
    ['{"schemaVersion":1,"commands":{}}', "corrupt"],
  ])("rejects invalid envelope %#", (serialized, code) => {
    expect(() => decodeQueue(serialized)).toThrowError(QueueSerializationError);
    try {
      decodeQueue(serialized);
    } catch (error) {
      expect(error).toMatchObject({ code });
    }
  });

  it.each([
    ["schemaVersion", 2],
    ["id", ""],
    ["clientId", 7],
    ["projectId", null],
    ["sequence", 0],
    ["sequence", -1],
    ["sequence", 1.5],
    ["sequence", Number.MAX_SAFE_INTEGER + 1],
    ["idempotencyKey", "wrong:1"],
    ["reviewedDigest", false],
    ["payloadHash", null],
    ["payload", undefined],
    ["state", "unknown"],
    ["createdAt", 123],
    ["attempts", -1],
    ["attempts", 1.5],
    ["attempts", Number.POSITIVE_INFINITY],
  ])("encode rejects corrupt command field %s=%s", (field, value) => {
    const invalid = { ...command("pending"), [field]: value };
    expectEncodeRejected(invalid);
  });

  it("rejects a non-object command", () => {
    expectEncodeRejected(null);
  });

  it.each([
    ["pending", problems.stale],
    ["sending", problems.offline],
    ["applied", problems.failed],
    ["stale", undefined],
    ["stale", problems.failed],
    ["failed", undefined],
    ["failed", problems.stale],
    ["reconciliation_required", undefined],
    ["reconciliation_required", problems.failed],
  ] as const)("rejects invalid persisted invariant %s/%#", (state, lastProblem) => {
    const invalid = { ...command(state), lastProblem };
    expectEncodeRejected(invalid);
  });

  it.each(["sending", "applied", "stale", "failed", "reconciliation_required"] as const)(
    "rejects %s with zero attempts as an impossible transition history",
    (state) => {
      const problem =
        state === "stale"
          ? problems.stale
          : state === "failed"
            ? problems.failed
            : state === "reconciliation_required"
              ? problems.reconciliation
              : undefined;
      expectEncodeRejected({ ...command(state, problem), attempts: 0 });
    },
  );

  it("rejects nonfinite sequence text during decoding", () => {
    const serialized = encodeQueue([command("pending")]).replace('"sequence":1', '"sequence":1e309');
    expect(() => decodeQueue(serialized)).toThrowError(QueueSerializationError);
  });

  it("rejects malformed problem details and stale changes", () => {
    const badProblem = {
      kind: "stale",
      code: "stale",
      title: "Stale",
      detail: "Changed",
      correlationId: 42,
      changes: [{ kind: "made_up", key: "x", reviewed_hash: 1, current_hash: null }],
    };
    expectEncodeRejected({ ...command("stale"), lastProblem: badProblem });
  });

  it("rejects non-array stale changes and unknown problem kinds", () => {
    expectEncodeRejected({
      ...command("stale"),
      lastProblem: { ...problems.stale, changes: null },
    });
    expectEncodeRejected({
      ...command("failed"),
      lastProblem: { ...problems.failed, kind: "mystery" },
    });
  });
});
