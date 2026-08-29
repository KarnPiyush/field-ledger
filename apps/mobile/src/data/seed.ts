import type { FieldLedgerDatabase } from "./database";
import type {
  AuditRecord,
  MobileDecisionCommand,
  ProjectRecord,
  ProviderJobRecord,
  ReviewBatchRecord,
  SessionRecord,
  SyncMetadataRecord,
} from "./types";

export const ACTIVE_PROJECT_ID = "project-riverside";

const session: SessionRecord = {
  key: "active",
  actorId: "actor-maya",
  actorName: "Maya Chen",
  clientId: "fieldledger-demo-phone",
  token: "local-demo-session",
  activeProjectId: ACTIVE_PROJECT_ID,
};

const project: ProjectRecord = {
  id: ACTIVE_PROJECT_ID,
  name: "Riverside Medical Center",
  description: "Electrical scope and change-control register",
  status: "active",
};

const evidence = {
  source: "Site Report 31",
  coordinate: "page 6, cost note",
  image: "/evidence/site-report-31-page-6.png",
};

const feeder = {
  id: "item-feeder",
  category: "Cost discrepancy",
  title: "Feeder cable allowance",
  summary: "The site report and change order disagree on the approved amount.",
  reason: "The change register uses the signed change order amount and keeps the site report value as a cited conflict.",
  before: "Feeder cable allowance: $42,500",
  after: "Feeder cable allowance: $47,250",
  removed: "$42,500",
  added: "$47,250",
  status: "pending",
  evidence,
};

const batches: ReviewBatchRecord[] = [
  {
    id: "batch-riverside",
    projectId: ACTIVE_PROJECT_ID,
    projectName: "Riverside Medical Center",
    title: "Change order 014 · Electrical scope",
    status: "pending",
    dependencyDigest: "reviewed-digest-riverside-v2",
    cachedAt: "2026-08-23T08:52:00",
    items: [
      feeder,
      { ...feeder, id: "item-date", category: "Schedule discrepancy", title: "Substantial completion date", summary: "The signed order moves completion by twelve days.", before: "Completion: 16 September", after: "Completion: 28 September", removed: "16 September", added: "28 September" },
      { ...feeder, id: "item-retention", category: "Contract term", title: "Retention rate", summary: "The executed amendment changes the retention percentage.", before: "Retention: 10%", after: "Retention: 5%", removed: "10%", added: "5%" },
      { ...feeder, id: "item-quantity", category: "Quantity variance", title: "Installed cable quantity", summary: "Site Report 31 records the measured installed quantity.", before: "Installed cable: 400 m", after: "Installed cable: 420 m", removed: "400 m", added: "420 m" },
      { ...feeder, id: "item-approval", category: "Approval reference", title: "Owner approval reference", summary: "The executed change order supplies the missing approval record.", before: "Owner approval: pending", after: "Owner approval: CO-014 signed", removed: "pending", added: "CO-014 signed" },
    ],
  },
  {
    id: "batch-north",
    projectId: ACTIVE_PROJECT_ID,
    projectName: "North Annex Fit-out",
    title: "Source changed while you were offline",
    status: "stale",
    dependencyDigest: "reviewed-digest-north-v2",
    cachedAt: "2026-08-23T08:48:00",
    items: [{ ...feeder, id: "item-north", title: "Branch circuit allowance" }],
  },
  {
    id: "batch-service",
    projectId: ACTIVE_PROJECT_ID,
    projectName: "Service Yard Upgrade",
    title: "Mixed decisions queued",
    status: "synced",
    dependencyDigest: "reviewed-digest-service-v1",
    cachedAt: "2026-08-23T08:44:00",
    items: [
      { ...feeder, id: "item-service-1" },
      { ...feeder, id: "item-service-2" },
      { ...feeder, id: "item-service-3" },
    ],
  },
];

const commands: MobileDecisionCommand[] = [
  {
    schemaVersion: 1,
    id: "queue-stale",
    clientId: "fieldledger-demo-phone",
    projectId: ACTIVE_PROJECT_ID,
    sequence: 1,
    idempotencyKey: "fieldledger-demo-phone:1",
    reviewedDigest: "reviewed-digest-north-v2",
    payloadHash: "demo-payload-stale",
    payload: { batchId: "batch-north", decisions: [{ item_id: "item-north", decision: "approve" }] },
    state: "stale",
    createdAt: "2026-08-23T09:38:00",
    attempts: 1,
    deduplicationKey: "batch-north:item-north",
    lastProblem: {
      kind: "stale",
      code: "stale-dependencies",
      title: "Review is out of date",
      detail: "Nothing was applied",
      changes: [{ key: "Site Report 31", kind: "source_version", reviewed_hash: "Version 2", current_hash: "Version 3" }],
    },
  },
  {
    schemaVersion: 1,
    id: "queue-date",
    clientId: "fieldledger-demo-phone",
    projectId: ACTIVE_PROJECT_ID,
    sequence: 2,
    idempotencyKey: "fieldledger-demo-phone:2",
    reviewedDigest: "reviewed-digest-riverside-v2",
    payloadHash: "demo-payload-date",
    payload: { batchId: "batch-riverside", decisions: [{ item_id: "item-date", decision: "reject", note: "Schedule narrative is incomplete." }] },
    state: "pending",
    createdAt: "2026-08-23T09:39:00",
    attempts: 0,
    deduplicationKey: "batch-riverside:item-date",
  },
  {
    schemaVersion: 1,
    id: "queue-retention",
    clientId: "fieldledger-demo-phone",
    projectId: ACTIVE_PROJECT_ID,
    sequence: 3,
    idempotencyKey: "fieldledger-demo-phone:3",
    reviewedDigest: "reviewed-digest-riverside-v2",
    payloadHash: "demo-payload-retention",
    payload: { batchId: "batch-riverside", decisions: [{ item_id: "item-retention", decision: "approve" }] },
    state: "pending",
    createdAt: "2026-08-23T09:40:00",
    attempts: 0,
    deduplicationKey: "batch-riverside:item-retention",
  },
];

const auditRows: AuditRecord[] = [
  { id: "audit-1", projectId: ACTIVE_PROJECT_ID, eventType: "Decision applied", actor: "Maya Chen", source: "Phone via FieldLedger relay", detail: "Approved feeder allowance in CO-013", correlationId: "corr-014-41", createdAt: "2026-08-23T09:48:00" },
  { id: "audit-2", projectId: ACTIVE_PROJECT_ID, eventType: "Review ready", actor: "Owen Patel", source: "SuperDocs provider relay", detail: "Prepared Change order 014 review round", correlationId: "corr-014-39", createdAt: "2026-08-23T09:31:00" },
  { id: "audit-3", projectId: ACTIVE_PROJECT_ID, eventType: "Source versioned", actor: "FieldLedger watcher", source: "Watched folder relay", detail: "Site Report 31 version 3 replaced version 2", correlationId: "corr-source-31", createdAt: "2026-08-23T09:12:00" },
];

const jobs: ProviderJobRecord[] = [
  { id: "job-processing", projectId: ACTIVE_PROJECT_ID, name: "Riverside register", status: "processing", detail: "continue_prompt", correlationId: "corr-job-1", updatedAt: "2026-08-23T09:55:00" },
  { id: "job-review", projectId: ACTIVE_PROJECT_ID, name: "North Annex review", status: "review_ready", detail: "PreviewReady", correlationId: "corr-job-2", updatedAt: "2026-08-23T09:54:00" },
  { id: "job-complete", projectId: ACTIVE_PROJECT_ID, name: "Service Yard export", status: "completed", detail: "Authoritative refresh complete", correlationId: "corr-job-3", updatedAt: "2026-08-23T09:53:00" },
  { id: "job-failed", projectId: ACTIVE_PROJECT_ID, name: "Warehouse run", status: "failed", detail: "Provider rejected request", correlationId: "corr-job-4", updatedAt: "2026-08-23T09:52:00" },
  { id: "job-cancelled", projectId: ACTIVE_PROJECT_ID, name: "Pier 7 run", status: "cancelled", detail: "Cancelled by operator", correlationId: "corr-job-5", updatedAt: "2026-08-23T09:51:00" },
  { id: "job-reconcile", projectId: ACTIVE_PROJECT_ID, name: "Library edit", status: "reconciliation_required", detail: "Response was lost after dispatch", correlationId: "corr-job-6", updatedAt: "2026-08-23T09:50:00" },
];

const sync: SyncMetadataRecord = {
  projectId: ACTIVE_PROJECT_ID,
  cachedAt: "2026-08-23T08:52:00",
  lastAttemptAt: "2026-08-23T08:52:00",
  connection: "offline",
};

export async function ensureDemoData(db: FieldLedgerDatabase): Promise<void> {
  if (await db.sessions.get("active")) return;
  await db.transaction(
    "rw",
    [db.sessions, db.projects, db.batches, db.offlineCommands, db.auditRows, db.syncMetadata, db.providerJobs],
    async () => {
      await db.sessions.put(session);
      await db.projects.put(project);
      await db.batches.bulkPut(batches);
      await db.offlineCommands.bulkPut(commands);
      await db.auditRows.bulkPut(auditRows);
      await db.syncMetadata.put(sync);
      await db.providerJobs.bulkPut(jobs);
    },
  );
}
