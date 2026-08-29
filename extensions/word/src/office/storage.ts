import { encodeQueue } from "@fieldledger/offline-queue";

export const DOCUMENT_ID_KEY = "fieldledger.word.document-id.v1";
export const QUEUE_KEY = "fieldledger.word.offline-queue.v1";

export interface OfficeAsyncResult {
  status: "succeeded" | "failed" | string;
  error?: { message?: string };
}

export interface OfficeSettings {
  get(name: string): unknown;
  set(name: string, value: unknown): void;
  remove(name: string): void;
  saveAsync(callback: (result: OfficeAsyncResult) => void): void;
}

export type OfficePersistenceErrorCode = "save_failed" | "save_timeout";

export class OfficePersistenceError extends Error {
  public constructor(
    readonly code: OfficePersistenceErrorCode,
    message: string,
  ) {
    super(message);
    this.name = "OfficePersistenceError";
  }
}

interface StorageOptions {
  timeoutMs: number;
  createId: () => string;
}

const defaults: StorageOptions = {
  timeoutMs: 5_000,
  createId: () => crypto.randomUUID(),
};

export class OfficeSettingsStorage {
  private readonly options: StorageOptions;

  public constructor(
    private readonly settings: OfficeSettings,
    options: Partial<StorageOptions> = {},
  ) {
    this.options = { ...defaults, ...options };
  }

  public async getOrCreateDocumentId(): Promise<string> {
    const existing = this.settings.get(DOCUMENT_ID_KEY);
    if (typeof existing === "string" && existing.length > 0) return existing;

    const documentId = this.options.createId();
    await this.setAndPersist(DOCUMENT_ID_KEY, documentId);
    return documentId;
  }

  public async loadQueue(): Promise<string> {
    const stored = this.settings.get(QUEUE_KEY);
    return typeof stored === "string" ? stored : encodeQueue([]);
  }

  public async saveQueue(serialized: string): Promise<void> {
    await this.setAndPersist(QUEUE_KEY, serialized);
  }

  public async resetQueue(): Promise<void> {
    await this.saveQueue(encodeQueue([]));
  }

  private persist(): Promise<void> {
    return new Promise((resolve, reject) => {
      let completed = false;
      const timeout = window.setTimeout(() => {
        if (completed) return;
        completed = true;
        reject(
          new OfficePersistenceError(
            "save_timeout",
            "Word did not confirm the document settings save in time.",
          ),
        );
      }, this.options.timeoutMs);

      this.settings.saveAsync((result) => {
        if (completed) return;
        completed = true;
        window.clearTimeout(timeout);
        if (result.status.toLowerCase() === "succeeded") {
          resolve();
          return;
        }
        reject(
          new OfficePersistenceError(
            "save_failed",
            result.error?.message ?? "Word could not save document settings.",
          ),
        );
      });
    });
  }

  private async setAndPersist(name: string, value: unknown): Promise<void> {
    const previous = this.settings.get(name);
    this.settings.set(name, value);
    try {
      await this.persist();
    } catch (error) {
      if (previous === undefined) this.settings.remove(name);
      else this.settings.set(name, previous);
      throw error;
    }
  }
}
