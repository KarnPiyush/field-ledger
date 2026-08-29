import { encodeQueue, transitionCommand } from "@fieldledger/offline-queue";
import { describe, expect, it } from "vitest";

import type {
  DocumentFingerprint,
  SerializedQueue,
  WordBridge,
  WordInstruction,
} from "../office/bridge";
import { DocumentQueueStore, WordQueueDataError } from "./queueStore";

const fingerprint: DocumentFingerprint = {
  documentId: "document-1",
  contentDigest: "content-a",
  selectionDigest: "selection-a",
  selectionPreview: "north annex",
};

const instruction: WordInstruction = {
  text: "Update the concrete quantity table",
  base: fingerprint,
  target: { kind: "document", preview: "Current document" },
};

class MemoryBridge implements WordBridge {
  public saves = 0;

  public constructor(public serialized: SerializedQueue = encodeQueue([])) {}

  public async ready(): Promise<void> {}
  public async getFingerprint(): Promise<DocumentFingerprint> {
    return fingerprint;
  }
  public async loadQueue(): Promise<SerializedQueue> {
    return this.serialized;
  }
  public async saveQueue(queue: SerializedQueue): Promise<void> {
    this.saves += 1;
    this.serialized = queue;
  }
  public async openReview(): Promise<void> {}
}

const meta = {
  id: "command-1",
  deduplicationKey: "draft-1",
  createdAt: "2026-08-23T12:00:00.000Z",
};

describe("document queue store", () => {
  it("stores the instruction and queue command atomically in one serialized value", async () => {
    const bridge = new MemoryBridge();
    const store = new DocumentQueueStore(bridge);

    const queued = await store.enqueue(instruction, meta);

    expect(bridge.saves).toBe(1);
    expect(queued).toMatchObject({
      id: "command-1",
      clientId: "document-1",
      projectId: "document-1",
      sequence: 1,
      idempotencyKey: "document-1:1",
      state: "pending",
      payload: instruction,
    });
  });

  it("restores commands when a new task pane instance uses the same document bridge", async () => {
    const bridge = new MemoryBridge();
    await new DocumentQueueStore(bridge).enqueue(instruction, meta);

    const restored = await new DocumentQueueStore(bridge).list();

    expect(restored).toHaveLength(1);
    expect(restored[0]?.payload.text).toBe(instruction.text);
  });

  it("reuses one idempotency key for concurrent duplicate submits", async () => {
    const bridge = new MemoryBridge();
    const store = new DocumentQueueStore(bridge);

    const [first, duplicate] = await Promise.all([
      store.enqueue(instruction, meta),
      store.enqueue(instruction, { ...meta, id: "command-duplicate" }),
    ]);

    expect(duplicate.id).toBe(first.id);
    expect(duplicate.idempotencyKey).toBe(first.idempotencyKey);
    expect(await store.list()).toHaveLength(1);
  });

  it("surfaces corrupt and unsupported queue data without discarding the raw value", async () => {
    const raw = '{"schemaVersion":99,"commands":[]}';
    const bridge = new MemoryBridge(raw);
    const store = new DocumentQueueStore(bridge);

    await expect(store.list()).rejects.toEqual(
      expect.objectContaining<Partial<WordQueueDataError>>({
        name: "WordQueueDataError",
        code: "unsupported_version",
      }),
    );
    await expect(store.exportRaw()).resolves.toBe(raw);
    expect(bridge.serialized).toBe(raw);

    await store.reset();
    await expect(store.list()).resolves.toEqual([]);
  });

  it("surfaces a canonical queue with an invalid Word payload without replaying it", async () => {
    const bridge = new MemoryBridge();
    const store = new DocumentQueueStore(bridge);
    await store.enqueue(instruction, meta);
    const envelope = JSON.parse(bridge.serialized) as { commands: Array<Record<string, unknown>> };
    envelope.commands[0]!.payload = {};
    bridge.serialized = JSON.stringify(envelope);

    await expect(store.list()).rejects.toMatchObject({
      name: "WordQueueDataError",
      code: "corrupt",
      raw: bridge.serialized,
    });
    await expect(store.exportRaw()).resolves.toBe(bridge.serialized);
  });

  it("refreshes a stale instruction in its original causal position", async () => {
    const bridge = new MemoryBridge();
    const store = new DocumentQueueStore(bridge);
    const first = await store.enqueue(instruction, meta);
    await store.enqueue(
      { ...instruction, text: "Later instruction" },
      { ...meta, id: "command-2", deduplicationKey: "draft-2" },
    );
    await store.compareAndSet(
      first.id,
      "pending",
      transitionCommand(transitionCommand(first, { kind: "begin_send" }), {
        kind: "stale",
        problem: {
          kind: "stale",
          code: "document-moved",
          title: "Needs refresh",
          detail: "Nothing was applied",
          changes: [],
        },
      }),
    );

    const refreshed = await store.refreshStale(first.id, {
      ...fingerprint,
      contentDigest: "content-current",
    });
    const commands = await store.list();

    expect(refreshed).toMatchObject({ sequence: 1, idempotencyKey: "document-1:1" });
    expect(commands.map((command) => command.sequence)).toEqual([1, 2]);
    expect(commands[0]?.payload.base.contentDigest).toBe("content-current");
  });

  it("rejects blank instructions before persisting anything", async () => {
    const bridge = new MemoryBridge();
    const store = new DocumentQueueStore(bridge);

    await expect(store.enqueue({ ...instruction, text: " \n\t " }, meta)).rejects.toThrow(
      "Instruction is required",
    );
    expect(bridge.saves).toBe(0);
  });
});
