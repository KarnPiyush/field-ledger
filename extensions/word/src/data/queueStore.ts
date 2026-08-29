import {
  canonicalRequestJson,
  decodeQueue,
  encodeQueue,
  makeIdempotencyKey,
  QueueSerializationError,
  transitionCommand,
  type QueueCommand,
  type QueueState,
  type QueueStore,
} from "@fieldledger/offline-queue";

import type {
  DocumentFingerprint,
  WordBridge,
  WordInstruction,
} from "../office/bridge";
import { sha256Text } from "../office/fingerprint";

export class WordQueueDataError extends Error {
  public constructor(
    readonly code: "corrupt" | "unsupported_version",
    readonly raw: string,
    message: string,
  ) {
    super(message);
    this.name = "WordQueueDataError";
  }
}

export interface EnqueueMetadata {
  id: string;
  deduplicationKey: string;
  createdAt: string;
}

function withoutProblem<T>(command: QueueCommand<T>): QueueCommand<T> {
  const { lastProblem: _lastProblem, ...next } = command;
  return next;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function isFingerprint(value: unknown): value is DocumentFingerprint {
  return (
    isRecord(value) &&
    typeof value.documentId === "string" &&
    typeof value.contentDigest === "string" &&
    typeof value.selectionDigest === "string" &&
    typeof value.selectionPreview === "string"
  );
}

function isWordInstruction(value: unknown): value is WordInstruction {
  return (
    isRecord(value) &&
    typeof value.text === "string" &&
    isFingerprint(value.base) &&
    isRecord(value.target) &&
    (value.target.kind === "document" || value.target.kind === "selection") &&
    typeof value.target.preview === "string"
  );
}

export class DocumentQueueStore implements QueueStore<WordInstruction> {
  private tail: Promise<void> = Promise.resolve();

  public constructor(private readonly bridge: WordBridge) {}

  public async list(): Promise<readonly QueueCommand<WordInstruction>[]> {
    const serialized = await this.bridge.loadQueue();
    try {
      return decodeQueue<WordInstruction>(serialized, isWordInstruction);
    } catch (error) {
      if (error instanceof QueueSerializationError) {
        throw new WordQueueDataError(error.code, serialized, error.message);
      }
      throw error;
    }
  }

  public async enqueue(
    instruction: WordInstruction,
    metadata: EnqueueMetadata,
  ): Promise<QueueCommand<WordInstruction>> {
    if (!instruction.text.trim()) throw new Error("Instruction is required");
    const payload: WordInstruction = { ...instruction, text: instruction.text.trim() };
    const payloadHash = await sha256Text(canonicalRequestJson(payload));
    return this.exclusive(async () => {
      const commands = [...(await this.list())];
      const existing = commands.find(
        (command) =>
          command.clientId === instruction.base.documentId &&
          command.deduplicationKey === metadata.deduplicationKey,
      );
      if (existing) return existing;
      const sequence =
        commands.reduce((largest, command) => Math.max(largest, command.sequence), 0) + 1;
      const command: QueueCommand<WordInstruction> = {
        schemaVersion: 1,
        id: metadata.id,
        clientId: instruction.base.documentId,
        projectId: instruction.base.documentId,
        sequence,
        idempotencyKey: makeIdempotencyKey(instruction.base.documentId, sequence),
        reviewedDigest: instruction.base.contentDigest,
        payloadHash,
        payload,
        state: "pending",
        createdAt: metadata.createdAt,
        attempts: 0,
        deduplicationKey: metadata.deduplicationKey,
      };
      await this.bridge.saveQueue(encodeQueue([...commands, command]));
      return command;
    });
  }

  public allocateAndAppend(): Promise<QueueCommand<WordInstruction>> {
    throw new Error("Use enqueue() so the Word instruction is hashed before allocation");
  }

  public compareAndSet(
    id: string,
    expected: QueueState,
    next: QueueCommand<WordInstruction>,
  ): Promise<boolean> {
    return this.exclusive(async () => {
      const commands = [...(await this.list())];
      const index = commands.findIndex((command) => command.id === id);
      if (index < 0 || commands[index]?.state !== expected) return false;
      commands[index] = next;
      await this.bridge.saveQueue(encodeQueue(commands));
      return true;
    });
  }

  public async exportRaw(): Promise<string> {
    return this.bridge.loadQueue();
  }

  public async reset(): Promise<void> {
    await this.exclusive(() => this.bridge.saveQueue(encodeQueue([])));
  }

  public async prepareReconciliation(id: string): Promise<void> {
    await this.replace(id, (command) => {
      if (command.state !== "reconciliation_required") return command;
      return { ...withoutProblem(command), state: "sending" };
    });
  }

  public async retryFailed(id: string): Promise<void> {
    await this.replace(id, (command) =>
      command.state === "failed" ? transitionCommand(command, { kind: "retry_failed" }) : command,
    );
  }

  public async refreshStale(
    id: string,
    base: DocumentFingerprint,
  ): Promise<QueueCommand<WordInstruction>> {
    return this.exclusive(async () => {
      const commands = [...(await this.list())];
      const stale = commands.find((command) => command.id === id && command.state === "stale");
      if (!stale) throw new Error("Stale instruction was not found");
      const payload = { ...stale.payload, base };
      const refreshed: QueueCommand<WordInstruction> = {
        ...withoutProblem(stale),
        id: crypto.randomUUID(),
        reviewedDigest: base.contentDigest,
        payloadHash: await sha256Text(canonicalRequestJson(payload)),
        payload,
        state: "pending",
        attempts: 0,
        createdAt: new Date().toISOString(),
        deduplicationKey: `refresh:${stale.id}:${stale.sequence}`,
      };
      const index = commands.findIndex((command) => command.id === id);
      commands[index] = refreshed;
      await this.bridge.saveQueue(encodeQueue(commands));
      return refreshed;
    });
  }

  private async replace(
    id: string,
    update: (command: QueueCommand<WordInstruction>) => QueueCommand<WordInstruction>,
  ): Promise<void> {
    await this.exclusive(async () => {
      const commands = [...(await this.list())];
      const index = commands.findIndex((command) => command.id === id);
      if (index < 0) throw new Error("Queued instruction was not found");
      const command = commands[index];
      if (!command) throw new Error("Queued instruction was not found");
      commands[index] = update(command);
      await this.bridge.saveQueue(encodeQueue(commands));
    });
  }

  private exclusive<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.tail.then(operation, operation);
    this.tail = result.then(
      () => undefined,
      () => undefined,
    );
    return result;
  }
}
