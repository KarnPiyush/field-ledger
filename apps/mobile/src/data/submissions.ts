import { canonicalRequestJson, type QueueCommand } from "@fieldledger/offline-queue";

import type { FieldLedgerDatabase } from "./database";

export interface SubmissionRecord {
  id: string;
  identity: string;
  token: string | null;
}

export function unpreparedSubmission(command: QueueCommand<unknown>): SubmissionRecord {
  return {
    id: command.id,
    identity: canonicalRequestJson({
      clientId: command.clientId,
      projectId: command.projectId,
      key: command.idempotencyKey,
      payloadHash: command.payloadHash,
      reviewedDigest: command.reviewedDigest,
      payload: command.payload,
    }),
    token: null,
  };
}

// Separate from queue snapshots: reducer writes and queue exports must not carry tokens.
export class SubmissionStore {
  public constructor(private readonly db: FieldLedgerDatabase) {}

  private async record(command: QueueCommand<unknown>): Promise<SubmissionRecord> {
    const record = await this.db.submissions.get(command.id);
    if (
      !record ||
      record.identity !== unpreparedSubmission(command).identity ||
      !(record.token === null || (typeof record.token === "string" && record.token.length > 0))
    ) {
      throw new Error("Original submission identity is missing or does not match this command");
    }
    return record;
  }

  public async read(command: QueueCommand<unknown>): Promise<string | null> {
    return (await this.record(command)).token;
  }

  public async save(command: QueueCommand<unknown>, token: string): Promise<string> {
    if (typeof token !== "string" || token.length === 0) {
      throw new Error("Confirmation token is missing");
    }
    // Never hold this transaction over a network request. Competing tabs use the winner.
    return this.db.transaction("rw", this.db.submissions, async () => {
      const record = await this.record(command);
      if (record.token !== null) return record.token;
      await this.db.submissions.put({ ...record, token });
      return token;
    });
  }
}
