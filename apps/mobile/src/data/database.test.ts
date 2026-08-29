import type { QueueCommand } from "@fieldledger/offline-queue";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FieldLedgerDatabase } from "./database";
import { DecisionStore, DexieQueueStore } from "./queueStore";
import { ReviewCache } from "./reviewCache";
import type { MobileDecisionPayload } from "./types";

const PROJECT_A = "project-riverside";
const PROJECT_B = "project-north";

describe("FieldLedger IndexedDB adapters", () => {
  let db: FieldLedgerDatabase;

  beforeEach(() => {
    db = new FieldLedgerDatabase(`fieldledger-test-${crypto.randomUUID()}`);
  });

  afterEach(async () => {
    await db.delete();
  });

  it("allocates immutable client sequence numbers and deduplicates a retried append", async () => {
    const store = new DexieQueueStore<MobileDecisionPayload>(db);
    const input = {
      id: "command-a",
      clientId: "phone-a",
      projectId: PROJECT_A,
      deduplicationKey: "batch-1:item-1:approve",
      reviewedDigest: "digest-a",
      payloadHash: "payload-a",
      payload: { batchId: "batch-1", decisions: [{ item_id: "item-1", decision: "approve" as const }] },
      createdAt: "2026-08-23T09:38:00.000Z",
    };

    const first = await store.allocateAndAppend(input);
    const replay = await store.allocateAndAppend({ ...input, id: "command-replay" });
    const second = await store.allocateAndAppend({
      ...input,
      id: "command-b",
      deduplicationKey: "batch-1:item-2:reject",
      payloadHash: "payload-b",
      payload: {
        batchId: "batch-1",
        decisions: [{ item_id: "item-2", decision: "reject" as const, note: "Value unsupported" }],
      },
    });

    expect(first.sequence).toBe(1);
    expect(replay.id).toBe(first.id);
    expect(second.sequence).toBe(2);
    expect((await store.list()).map((command) => command.id)).toEqual(["command-a", "command-b"]);
  });

  it("keeps incomplete drafts local and enqueues one ordered batch command when all items are decided", async () => {
    const decisions = new DecisionStore(db, "phone-a");

    const incomplete = await decisions.save({
      projectId: PROJECT_A,
      batchId: "batch-1",
      itemId: "item-1",
      decision: "reject",
      note: "The cited report does not support this amount.",
      batchItemIds: ["item-1", "item-2"],
      reviewedDigest: "digest-a",
      createdAt: "2026-08-23T09:39:00.000Z",
    });
    expect(incomplete).toBeNull();
    expect(await db.offlineCommands.count()).toBe(0);

    await decisions.save({
      projectId: PROJECT_A,
      batchId: "batch-1",
      itemId: "item-1",
      decision: "approve",
      batchItemIds: ["item-1", "item-2"],
      reviewedDigest: "digest-a",
      createdAt: "2026-08-23T09:40:00.000Z",
    });
    const command = await decisions.save({
      projectId: PROJECT_A,
      batchId: "batch-1",
      itemId: "item-2",
      decision: "reject",
      note: "Completion evidence is incomplete.",
      batchItemIds: ["item-1", "item-2"],
      reviewedDigest: "digest-a",
      createdAt: "2026-08-23T09:41:00.000Z",
    });

    expect(command).toMatchObject({ projectId: PROJECT_A, sequence: 1, state: "pending" });
    expect(await db.drafts.get("batch-1:item-1")).toMatchObject({ decision: "approve" });
    expect(await db.offlineCommands.get(command!.id)).toMatchObject({
      deduplicationKey: "batch-1:digest-a:decisions",
      payload: {
        batchId: "batch-1",
        decisions: [
          { item_id: "item-1", decision: "approve" },
          { item_id: "item-2", decision: "reject", note: "Completion evidence is incomplete." },
        ],
      },
    });
    expect(await db.offlineCommands.count()).toBe(1);
  });

  it("rolls back both records and rejects when persistence fails", async () => {
    db.offlineCommands.hook("creating", () => {
      throw new Error("storage unavailable");
    });
    const decisions = new DecisionStore(db, "phone-a");

    await expect(
      decisions.save({
        projectId: PROJECT_A,
        batchId: "batch-1",
        itemId: "item-1",
        decision: "approve",
        batchItemIds: ["item-1"],
        reviewedDigest: "digest-a",
        createdAt: "2026-08-23T09:40:00.000Z",
      }),
    ).rejects.toThrow("storage unavailable");

    expect(await db.drafts.count()).toBe(0);
    expect(await db.offlineCommands.count()).toBe(0);
  });

  it("never returns another project's cached batches or audit rows", async () => {
    await db.batches.bulkPut([
      { id: "batch-a", projectId: PROJECT_A, projectName: "Riverside", title: "CO-014", status: "pending", dependencyDigest: "a", items: [], cachedAt: "2026-08-23T08:52:00Z" },
      { id: "batch-b", projectId: PROJECT_B, projectName: "North", title: "Fit-out", status: "pending", dependencyDigest: "b", items: [], cachedAt: "2026-08-23T08:53:00Z" },
    ]);
    await db.auditRows.bulkPut([
      { id: "audit-a", projectId: PROJECT_A, eventType: "decision.applied", actor: "Maya Chen", source: "phone", correlationId: "corr-a", createdAt: "2026-08-23T08:54:00Z", detail: "Applied one decision" },
      { id: "audit-b", projectId: PROJECT_B, eventType: "decision.applied", actor: "Noah Singh", source: "relay", correlationId: "corr-b", createdAt: "2026-08-23T08:55:00Z", detail: "Applied another decision" },
    ]);
    const cache = new ReviewCache(db);

    expect((await cache.listBatches(PROJECT_A)).map((batch) => batch.id)).toEqual(["batch-a"]);
    expect((await cache.listAudit(PROJECT_A)).map((row) => row.id)).toEqual(["audit-a"]);
  });

  it("implements compare-and-set without overwriting a competing queue state", async () => {
    const store = new DexieQueueStore<MobileDecisionPayload>(db);
    const pending = await store.allocateAndAppend({
      id: "command-a",
      clientId: "phone-a",
      projectId: PROJECT_A,
      deduplicationKey: "decision-a",
      reviewedDigest: "digest-a",
      payloadHash: "payload-a",
      payload: { batchId: "batch-1", decisions: [{ item_id: "item-1", decision: "approve" }] },
      createdAt: "2026-08-23T09:38:00.000Z",
    });
    const sending: QueueCommand<MobileDecisionPayload> = { ...pending, state: "sending", attempts: 1 };

    expect(await store.compareAndSet(pending.id, "pending", sending)).toBe(true);
    expect(await store.compareAndSet(pending.id, "pending", { ...sending, state: "applied" })).toBe(false);
    expect((await db.offlineCommands.get(pending.id))?.state).toBe("sending");
  });

  it("rejects a canonical IndexedDB command with an invalid mobile decision payload", async () => {
    const store = new DexieQueueStore<MobileDecisionPayload>(db);
    await db.offlineCommands.put({
      schemaVersion: 1,
      id: "corrupt-command",
      clientId: "phone-a",
      projectId: PROJECT_A,
      sequence: 1,
      idempotencyKey: "phone-a:1",
      reviewedDigest: "digest-a",
      payloadHash: "payload-a",
      payload: {} as MobileDecisionPayload,
      state: "pending",
      createdAt: "2026-08-23T09:38:00.000Z",
      attempts: 0,
    });

    await expect(store.list()).rejects.toMatchObject({
      name: "QueueSerializationError",
      code: "corrupt",
    });
  });
});
