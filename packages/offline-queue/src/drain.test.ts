import { describe, expect, it } from "vitest";

import {
  drainInOrder,
  makeIdempotencyKey,
  reconcileBlockedCommand,
  type DecisionRequestDto,
  type NewCommand,
  type QueueCommand,
  type QueueProblem,
  type QueueState,
  type QueueStore,
  type SendResult,
  type Sender,
} from "./index.js";

const payload: DecisionRequestDto = {
  confirmation_token: "confirmation",
  decisions: [{ item_id: "item-1", decision: "approve", note: "Approved offline" }],
};

const offlineProblem = {
  kind: "offline",
  code: "offline",
  title: "Offline",
  detail: "No bytes were sent",
} as const satisfies QueueProblem;

const staleProblem = {
  kind: "stale",
  code: "dependency-stale",
  title: "Review is stale",
  detail: "A source changed",
  correlationId: "corr-stale",
  changes: [
    {
      kind: "source_version" as const,
      key: "source:1",
      reviewed_hash: "sha256:old",
      current_hash: "sha256:new",
    },
  ],
} as const satisfies QueueProblem;

const failedProblem = {
  kind: "failed",
  code: "invalid-review",
  title: "Review rejected",
  detail: "The decision was invalid",
  correlationId: "corr-failed",
} as const satisfies QueueProblem;

const reconciliationProblem = {
  kind: "reconciliation_required",
  code: "response-lost",
  title: "Check submission",
  detail: "The response was lost after dispatch",
  correlationId: "corr-reconcile",
} as const satisfies QueueProblem;

function command(
  sequence: number,
  state: QueueState = "pending",
  overrides: Partial<QueueCommand<DecisionRequestDto>> = {},
): QueueCommand<DecisionRequestDto> {
  const attempts = state === "pending" ? 0 : 1;
  return {
    schemaVersion: 1,
    id: `command-${sequence}`,
    clientId: "client-a",
    projectId: "project-a",
    sequence,
    idempotencyKey: makeIdempotencyKey("client-a", sequence),
    reviewedDigest: "sha256:reviewed",
    payloadHash: `sha256:payload-${sequence}`,
    payload,
    state,
    createdAt: `2026-01-0${sequence}T00:00:00Z`,
    attempts,
    ...overrides,
  };
}

class MemoryStore implements QueueStore<DecisionRequestDto> {
  readonly casCalls: Array<readonly [string, QueueState, QueueState]> = [];
  private readonly commands: QueueCommand<DecisionRequestDto>[];

  constructor(commands: readonly QueueCommand<DecisionRequestDto>[]) {
    this.commands = commands.map((item) => ({ ...item }));
  }

  async list(): Promise<readonly QueueCommand<DecisionRequestDto>[]> {
    return this.commands.map((item) => ({ ...item }));
  }

  async allocateAndAppend(
    input: NewCommand<DecisionRequestDto>,
  ): Promise<QueueCommand<DecisionRequestDto>> {
    const duplicate = this.commands.find(
      (item) => item.clientId === input.clientId && item.deduplicationKey === input.deduplicationKey,
    );
    if (duplicate !== undefined) return duplicate;
    const sequence =
      Math.max(
        0,
        ...this.commands
          .filter((item) => item.clientId === input.clientId)
          .map((item) => item.sequence),
      ) + 1;
    const appended: QueueCommand<DecisionRequestDto> = {
      schemaVersion: 1,
      ...input,
      sequence,
      idempotencyKey: makeIdempotencyKey(input.clientId, sequence),
      state: "pending",
      attempts: 0,
    };
    this.commands.push(appended);
    return appended;
  }

  async compareAndSet(
    id: string,
    expected: QueueState,
    next: QueueCommand<DecisionRequestDto>,
  ): Promise<boolean> {
    this.casCalls.push([id, expected, next.state]);
    const index = this.commands.findIndex((item) => item.id === id);
    if (index < 0 || this.commands[index]?.state !== expected) return false;
    this.commands[index] = { ...next };
    return true;
  }
}

class ContendedMemoryStore extends MemoryStore {
  private callCount = 0;

  constructor(
    commands: readonly QueueCommand<DecisionRequestDto>[],
    private readonly failOnCall: number,
  ) {
    super(commands);
  }

  override async compareAndSet(
    id: string,
    expected: QueueState,
    next: QueueCommand<DecisionRequestDto>,
  ): Promise<boolean> {
    this.callCount += 1;
    if (this.callCount === this.failOnCall) return false;
    return super.compareAndSet(id, expected, next);
  }
}

function sender(results: readonly SendResult[]): Sender<DecisionRequestDto> & {
  sent: string[];
  reconciled: string[];
} {
  let index = 0;
  return {
    sent: [],
    reconciled: [],
    async send(item) {
      this.sent.push(item.idempotencyKey);
      const result = results[index++];
      if (result === undefined) throw new Error("Missing test result");
      return result;
    },
    async reconcile(item) {
      this.reconciled.push(item.idempotencyKey);
      const result = results[index++];
      if (result === undefined) throw new Error("Missing test result");
      return result;
    },
  };
}

const applied = (replayed = false): SendResult => ({
  kind: "applied",
  response: { batch_id: "batch-1", status: "completed", replayed },
});

describe("QueueStore allocation contract", () => {
  it("allocates per-client monotonic sequences atomically and reuses duplicate UI events", async () => {
    const store = new MemoryStore([]);
    const input: NewCommand<DecisionRequestDto> = {
      id: "event-1",
      clientId: "client-a",
      projectId: "project-a",
      deduplicationKey: "review-submit:batch-1",
      reviewedDigest: "sha256:reviewed",
      payloadHash: "sha256:payload",
      payload,
      createdAt: "2026-01-01T00:00:00Z",
    };

    const [first, duplicate] = await Promise.all([
      store.allocateAndAppend(input),
      store.allocateAndAppend(input),
    ]);
    const second = await store.allocateAndAppend({
      ...input,
      id: "event-2",
      deduplicationKey: "review-submit:batch-2",
    });

    expect(first).toEqual(duplicate);
    expect(first.idempotencyKey).toBe("client-a:1");
    expect(second.idempotencyKey).toBe("client-a:2");
    expect(await store.list()).toHaveLength(2);
  });
});

describe("drainInOrder", () => {
  it("orders by client and sequence only despite UUID, insertion, DST, timezone, and clock rollback", async () => {
    const store = new MemoryStore([
      command(1, "pending", {
        id: "yyyy",
        clientId: "client-b",
        idempotencyKey: "client-b:1",
        createdAt: "1900-01-01T00:00:00-12:00",
      }),
      command(2, "pending", {
        id: "0000",
        createdAt: "2026-11-01T01:15:00-05:00",
      }),
      command(1, "applied", { id: "zzzz", createdAt: "2099-01-01T00:00:00+14:00" }),
    ]);
    const transport = sender([applied(), applied()]);

    await expect(drainInOrder(store, transport)).resolves.toEqual({
      status: "drained",
      applied: 2,
      processed: 2,
    });
    expect(transport.sent).toEqual(["client-a:2", "client-b:1"]);
  });

  it("applies an idempotent server replay exactly once locally", async () => {
    const store = new MemoryStore([command(1)]);
    const transport = sender([applied(true)]);

    const summary = await drainInOrder(store, transport);

    expect(summary).toEqual({ status: "drained", applied: 1, processed: 1 });
    expect((await store.list())[0]?.state).toBe("applied");
    expect(store.casCalls).toEqual([
      ["command-1", "pending", "sending"],
      ["command-1", "sending", "applied"],
    ]);
  });

  it("skips applied histories that are already in ascending client order", async () => {
    const store = new MemoryStore([
      command(1, "applied"),
      command(1, "applied", {
        id: "client-b-history",
        clientId: "client-b",
        idempotencyKey: "client-b:1",
      }),
    ]);

    await expect(drainInOrder(store, sender([]))).resolves.toEqual({
      status: "drained",
      applied: 0,
      processed: 0,
    });
  });

  it("skips superseded stale history and drains the next command", async () => {
    const store = new MemoryStore([command(1, "superseded"), command(2)]);
    const transport = sender([applied()]);

    await expect(drainInOrder(store, transport)).resolves.toEqual({
      status: "drained",
      applied: 1,
      processed: 1,
    });
    expect(transport.sent).toEqual(["client-a:2"]);
  });

  it("recovers a crash in sending through reconcile with the same key before later sends", async () => {
    const store = new MemoryStore([command(2), command(1, "sending")]);
    const transport = sender([applied(true), applied()]);

    await drainInOrder(store, transport);

    expect(transport.reconciled).toEqual(["client-a:1"]);
    expect(transport.sent).toEqual(["client-a:2"]);
  });

  it("allows concurrent drains to send a pending sequence at most once", async () => {
    const store = new MemoryStore([command(1)]);
    const transport = sender([applied()]);

    const summaries = await Promise.all([
      drainInOrder(store, transport),
      drainInOrder(store, transport),
    ]);

    expect(transport.sent).toEqual(["client-a:1"]);
    expect(summaries.map((item) => item.status).sort()).toEqual(["contended", "drained"]);
  });

  it.each([
    [
      { kind: "offline", problem: offlineProblem },
      "offline",
      "pending",
      offlineProblem,
    ],
    [{ kind: "stale", problem: staleProblem }, "stale", "stale", staleProblem],
    [{ kind: "failed", problem: failedProblem }, "failed", "failed", failedProblem],
    [
      { kind: "reconciliation_required", problem: reconciliationProblem },
      "reconciliation_required",
      "reconciliation_required",
      reconciliationProblem,
    ],
  ] as const)(
    "stops on %s without touching the next sequence",
    async (result, status, state, expectedProblem) => {
      const store = new MemoryStore([command(2), command(1)]);
      const transport = sender([result]);

      await expect(drainInOrder(store, transport)).resolves.toEqual({
        status,
        applied: 0,
        processed: 1,
        commandId: "command-1",
      });
      const stored = await store.list();
      expect(stored.find((item) => item.id === "command-1")).toMatchObject({
        state,
        lastProblem: expectedProblem,
      });
      expect(stored.find((item) => item.id === "command-2")?.state).toBe("pending");
      expect(transport.sent).toEqual(["client-a:1"]);
    },
  );

  it.each([
    ["stale", staleProblem],
    ["failed", failedProblem],
    ["reconciliation_required", reconciliationProblem],
  ] as const)("stops at an existing %s command", async (state, problem) => {
    const store = new MemoryStore([command(2), command(1, state, { lastProblem: problem })]);
    const transport = sender([]);

    await expect(drainInOrder(store, transport)).resolves.toEqual({
      status: state,
      applied: 0,
      processed: 0,
      commandId: "command-1",
    });
    expect(transport.sent).toEqual([]);
  });

  it("detects a same-key different-payload conflict locally and never sends", async () => {
    const store = new MemoryStore([
      command(1, "applied"),
      command(2, "pending", {
        idempotencyKey: "client-a:1",
        payloadHash: "sha256:different",
      }),
    ]);
    const transport = sender([]);

    await expect(drainInOrder(store, transport)).resolves.toEqual({
      status: "failed",
      applied: 0,
      processed: 1,
      commandId: "command-2",
    });
    expect(transport.sent).toEqual([]);
    expect((await store.list())[1]).toMatchObject({
      state: "failed",
      lastProblem: {
        kind: "failed",
        code: "idempotency-conflict",
      },
    });
  });

  it("detects a conflicting recovered sending command without reconciling it", async () => {
    const store = new MemoryStore([
      command(1, "applied"),
      command(2, "sending", {
        idempotencyKey: "client-a:1",
        payloadHash: "sha256:different",
      }),
    ]);
    const transport = sender([]);

    const summary = await drainInOrder(store, transport);

    expect(summary.status).toBe("failed");
    expect(transport.reconciled).toEqual([]);
  });

  it("stops contended when a conflict transition loses its CAS", async () => {
    const store = new MemoryStore([
      command(1, "applied"),
      command(2, "sending", {
        idempotencyKey: "client-a:1",
        payloadHash: "sha256:different",
      }),
    ]);
    store.compareAndSet = async () => false;

    await expect(drainInOrder(store, sender([]))).resolves.toEqual({
      status: "contended",
      applied: 0,
      processed: 1,
      commandId: "command-2",
    });
  });

  it("does not swallow an unexpected sender exception and leaves recovery in sending", async () => {
    const store = new MemoryStore([command(1)]);
    const transport: Sender<DecisionRequestDto> = {
      async send() {
        throw new Error("transport exploded");
      },
      async reconcile() {
        return applied();
      },
    };

    await expect(drainInOrder(store, transport)).rejects.toThrow("transport exploded");
    expect((await store.list())[0]?.state).toBe("sending");
  });

  it("stops when the final CAS loses ownership after a send", async () => {
    const store = new MemoryStore([command(1), command(2)]);
    const original = store.compareAndSet.bind(store);
    let calls = 0;
    store.compareAndSet = async (...args) => {
      calls += 1;
      if (calls === 2) return false;
      return original(...args);
    };

    await expect(drainInOrder(store, sender([applied()]))).resolves.toEqual({
      status: "contended",
      applied: 0,
      processed: 1,
      commandId: "command-1",
    });
  });

  it("returns drained for an empty queue", async () => {
    await expect(drainInOrder(new MemoryStore([]), sender([]))).resolves.toEqual({
      status: "drained",
      applied: 0,
      processed: 0,
    });
  });

});

describe("reconcileBlockedCommand", () => {
  it("rejects commands that are not blocked for reconciliation", async () => {
    const pending = command(1);
    const transport = sender([applied()]);

    await expect(
      reconcileBlockedCommand(new MemoryStore([pending]), transport, pending),
    ).rejects.toThrow("Only reconciliation-required commands can be reconciled manually");
    expect(transport.reconciled).toEqual([]);
  });

  it.each([
    [{ kind: "offline", problem: offlineProblem }, "pending", offlineProblem],
    [{ kind: "stale", problem: staleProblem }, "stale", staleProblem],
    [{ kind: "failed", problem: failedProblem }, "failed", failedProblem],
    [
      { kind: "reconciliation_required", problem: reconciliationProblem },
      "reconciliation_required",
      reconciliationProblem,
    ],
  ] as const)(
    "persists a %s reconciliation result through sending",
    async (result, state, expectedProblem) => {
      const blocked = command(1, "reconciliation_required", {
        lastProblem: reconciliationProblem,
      });
      const store = new MemoryStore([blocked]);
      const transport = sender([result]);

      await expect(reconcileBlockedCommand(store, transport, blocked)).resolves.toEqual(result);

      expect((await store.list())[0]).toMatchObject({ state, lastProblem: expectedProblem });
      expect(store.casCalls).toEqual([
        ["command-1", "reconciliation_required", "sending"],
        ["command-1", "sending", state],
      ]);
      expect(transport.reconciled).toEqual(["client-a:1"]);
    },
  );

  it.each([
    [1, "before reconciliation", 0],
    [2, "during reconciliation", 1],
  ] as const)(
    "rejects a command changed %s",
    async (failOnCall, phase, expectedReconciliations) => {
      const blocked = command(1, "reconciliation_required", {
        lastProblem: reconciliationProblem,
      });
      const store = new ContendedMemoryStore([blocked], failOnCall);
      const transport = sender([applied()]);

      await expect(reconcileBlockedCommand(store, transport, blocked)).rejects.toThrow(
        `Command changed ${phase}`,
      );
      expect(transport.reconciled).toHaveLength(expectedReconciliations);
    },
  );
});
