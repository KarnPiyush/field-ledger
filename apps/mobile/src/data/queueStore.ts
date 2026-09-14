import Dexie from "dexie";
import {
  canonicalRequestJson,
  decodeQueue,
  makeIdempotencyKey,
  type NewCommand,
  type QueueCommand,
  type QueueState,
  type QueueStore,
} from "@fieldledger/offline-queue";

import type { FieldLedgerDatabase } from "./database";
import { unpreparedSubmission } from "./submissions";
import type {
  MobileDecisionCommand,
  MobileDecisionPayload,
  SaveDecisionInput,
} from "./types";

async function sha256(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const hash = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(hash)].map((part) => part.toString(16).padStart(2, "0")).join("");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isMobileDecisionPayload(value: unknown): value is MobileDecisionPayload {
  return (
    isRecord(value) &&
    typeof value.batchId === "string" &&
    value.batchId.length > 0 &&
    Array.isArray(value.decisions) &&
    value.decisions.length > 0 &&
    value.decisions.every(
      (decision) =>
        isRecord(decision) &&
        typeof decision.item_id === "string" &&
        decision.item_id.length > 0 &&
        (decision.decision === "approve" || decision.decision === "reject") &&
        (decision.note === undefined ||
          decision.note === null ||
          typeof decision.note === "string"),
    )
  );
}

async function allocate<T>(db: FieldLedgerDatabase, input: NewCommand<T>): Promise<QueueCommand<T>> {
  const existing = await db.offlineCommands
    .where("[clientId+deduplicationKey]")
    .equals([input.clientId, input.deduplicationKey])
    .first();
  if (existing !== undefined) return existing as QueueCommand<T>;

  const prior = await db.offlineCommands
    .where("[clientId+sequence]")
    .between([input.clientId, Dexie.minKey], [input.clientId, Dexie.maxKey])
    .last();
  const sequence = (prior?.sequence ?? 0) + 1;
  const command: QueueCommand<T> = {
    schemaVersion: 1,
    ...input,
    sequence,
    idempotencyKey: makeIdempotencyKey(input.clientId, sequence),
    state: "pending",
    attempts: 0,
  };
  await db.offlineCommands.add(command as MobileDecisionCommand);
  await db.submissions.add(unpreparedSubmission(command));
  return command;
}

export class DexieQueueStore<T = MobileDecisionPayload> implements QueueStore<T> {
  public constructor(private readonly db: FieldLedgerDatabase) {}

  public async list(): Promise<readonly QueueCommand<T>[]> {
    const commands = await this.db.offlineCommands.toArray();
    return decodeQueue<T>(
      JSON.stringify({ schemaVersion: 1, commands }),
      isMobileDecisionPayload as (value: unknown) => value is T,
    );
  }

  public async allocateAndAppend(input: NewCommand<T>): Promise<QueueCommand<T>> {
    return this.db.transaction("rw", [this.db.offlineCommands, this.db.submissions], () => allocate(this.db, input));
  }

  public async compareAndSet(
    id: string,
    expected: QueueState,
    next: QueueCommand<T>,
  ): Promise<boolean> {
    return this.db.transaction("rw", this.db.offlineCommands, async () => {
      const current = await this.db.offlineCommands.get(id);
      if (current?.state !== expected) return false;
      await this.db.offlineCommands.put(next as MobileDecisionCommand);
      return true;
    });
  }
}

export class DecisionStore {
  public constructor(
    private readonly db: FieldLedgerDatabase,
    private readonly clientId: string,
  ) {}

  public async save(input: SaveDecisionInput): Promise<MobileDecisionCommand | null> {
    if (input.decision === "reject" && !input.note?.trim()) {
      throw new Error("A rejection note is required");
    }
    const uniqueItemIds = [...new Set(input.batchItemIds)];
    if (uniqueItemIds.length === 0 || uniqueItemIds.length !== input.batchItemIds.length) {
      throw new Error("The authoritative batch item list is invalid");
    }
    if (!uniqueItemIds.includes(input.itemId)) {
      throw new Error("The decision item is not in the authoritative batch");
    }
    const deduplicationKey = `${input.batchId}:${input.reviewedDigest}:decisions`;

    return this.db.transaction("rw", [this.db.drafts, this.db.offlineCommands, this.db.submissions], async () => {
      const existing = await this.db.offlineCommands
        .where("[clientId+deduplicationKey]")
        .equals([this.clientId, deduplicationKey])
        .first();
      if (existing) return existing;

      await this.db.drafts.put({
        id: `${input.batchId}:${input.itemId}`,
        projectId: input.projectId,
        batchId: input.batchId,
        itemId: input.itemId,
        decision: input.decision,
        ...(input.note?.trim() ? { note: input.note.trim() } : {}),
        createdAt: input.createdAt,
      });
      const drafts = await this.db.drafts.where("batchId").equals(input.batchId).toArray();
      const draftByItem = new Map(
        drafts
          .filter((draft) => draft.projectId === input.projectId && uniqueItemIds.includes(draft.itemId))
          .map((draft) => [draft.itemId, draft]),
      );
      if (!uniqueItemIds.every((itemId) => draftByItem.has(itemId))) return null;

      const payload: MobileDecisionPayload = {
        batchId: input.batchId,
        decisions: uniqueItemIds.map((itemId) => {
          const draft = draftByItem.get(itemId)!;
          return {
            item_id: itemId,
            decision: draft.decision,
            ...(draft.note?.trim() ? { note: draft.note.trim() } : {}),
          };
        }),
      };
      const payloadHash = await Dexie.waitFor(sha256(canonicalRequestJson(payload)));
      return allocate(this.db, {
        id: crypto.randomUUID(),
        clientId: this.clientId,
        projectId: input.projectId,
        deduplicationKey,
        reviewedDigest: input.reviewedDigest,
        payloadHash,
        payload,
        createdAt: input.createdAt,
      });
    });
  }
}
