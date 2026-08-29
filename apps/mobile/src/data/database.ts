import Dexie, { type EntityTable } from "dexie";

import type {
  AuditRecord,
  DraftRecord,
  MobileDecisionCommand,
  ProjectRecord,
  ProviderJobRecord,
  ReviewBatchRecord,
  SessionRecord,
  SyncMetadataRecord,
} from "./types";

export class FieldLedgerDatabase extends Dexie {
  sessions!: EntityTable<SessionRecord, "key">;
  projects!: EntityTable<ProjectRecord, "id">;
  batches!: EntityTable<ReviewBatchRecord, "id">;
  drafts!: EntityTable<DraftRecord, "id">;
  offlineCommands!: EntityTable<MobileDecisionCommand, "id">;
  auditRows!: EntityTable<AuditRecord, "id">;
  syncMetadata!: EntityTable<SyncMetadataRecord, "projectId">;
  providerJobs!: EntityTable<ProviderJobRecord, "id">;

  public constructor(name = "fieldledger-mobile") {
    super(name);

    this.version(1).stores({
      sessions: "&key",
      projects: "&id",
      batches: "&id, projectId, [projectId+status]",
      drafts: "&id, projectId, batchId, itemId",
      offlineCommands: "&id, [clientId+sequence], &[clientId+deduplicationKey], projectId, state",
      syncMetadata: "&projectId",
    });

    this.version(2).stores({
      sessions: "&key",
      projects: "&id",
      batches: "&id, projectId, [projectId+status]",
      drafts: "&id, projectId, batchId, itemId",
      offlineCommands: "&id, [clientId+sequence], &[clientId+deduplicationKey], projectId, state",
      auditRows: "&id, projectId, [projectId+createdAt]",
      syncMetadata: "&projectId",
      providerJobs: "&id, projectId, [projectId+status]",
    });
  }
}

export const fieldLedgerDb = new FieldLedgerDatabase();
