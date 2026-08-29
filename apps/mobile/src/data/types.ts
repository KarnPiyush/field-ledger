import type { components } from "@fieldledger/api-client";
import type { QueueCommand } from "@fieldledger/offline-queue";

export type ReviewDecisionKind = components["schemas"]["ReviewDecisionKind"];

export interface SessionRecord {
  key: "active";
  actorId: string;
  actorName: string;
  clientId: string;
  token: string;
  activeProjectId: string;
}

export interface ProjectRecord {
  id: string;
  name: string;
  description: string;
  status: string;
}

export interface EvidenceRecord {
  source: string;
  coordinate: string;
  image: string;
}

export interface ReviewItemRecord {
  id: string;
  category: string;
  title: string;
  summary: string;
  reason: string;
  before: string;
  after: string;
  removed: string;
  added: string;
  status: string;
  evidence: EvidenceRecord;
}

export interface ReviewBatchRecord {
  id: string;
  projectId: string;
  projectName: string;
  title: string;
  status: string;
  dependencyDigest: string;
  items: ReviewItemRecord[];
  cachedAt: string;
}

export interface DraftRecord {
  id: string;
  projectId: string;
  batchId: string;
  itemId: string;
  decision: ReviewDecisionKind;
  note?: string;
  createdAt: string;
}

export interface MobileDecisionPayload {
  batchId: string;
  decisions: components["schemas"]["ReviewDecision"][];
}

export type MobileDecisionCommand = QueueCommand<MobileDecisionPayload>;

export interface AuditRecord {
  id: string;
  projectId: string;
  eventType: string;
  actor: string;
  source: string;
  detail: string;
  correlationId: string;
  createdAt: string;
}

export interface SyncMetadataRecord {
  projectId: string;
  cachedAt: string;
  lastAttemptAt: string;
  connection: "online" | "offline" | "error";
  error?: string;
}

export interface ProviderJobRecord {
  id: string;
  projectId: string;
  name: string;
  status: string;
  detail: string;
  correlationId: string;
  updatedAt: string;
}

export interface SaveDecisionInput {
  projectId: string;
  batchId: string;
  itemId: string;
  batchItemIds: string[];
  decision: ReviewDecisionKind;
  note?: string;
  reviewedDigest: string;
  createdAt: string;
}
