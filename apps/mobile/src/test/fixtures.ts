import type { AuditPageProps } from "../pages/AuditPage";
import type { InboxPageProps } from "../pages/InboxPage";
import type { OpsPageProps } from "../pages/OpsPage";
import type { QueuePageProps } from "../pages/QueuePage";
import type { ReviewPageProps } from "../pages/ReviewPage";

const inbox: InboxPageProps = {
  metrics: { pending: 7, queued: 3, stale: 1 },
  cachedAt: "2026-08-23T08:52:00",
  batches: [
    { id: "batch-riverside", projectName: "Riverside Medical Center", title: "Change order 014 · Electrical scope", detail: "Due today", count: 5, status: "pending" },
    { id: "batch-north", projectName: "North Annex Fit-out", title: "Source changed while you were offline", detail: "Refresh before review", count: 1, status: "stale" },
  ],
  connection: "offline",
  pending: false,
  onRefresh: async () => undefined,
};

const review: Omit<ReviewPageProps, "onDecision"> = {
  projectName: "Riverside Medical Center",
  batchTitle: "Change order 014",
  progress: { current: 2, total: 5 },
  item: {
    id: "item-feeder",
    category: "Cost discrepancy",
    title: "Feeder cable allowance",
    summary: "The site report and change order disagree on the approved amount.",
    reason: "The change register uses the signed change order amount and keeps the site report value as a cited conflict.",
    source: "Site Report 31",
    coordinate: "page 6, cost note",
    before: "Feeder cable allowance: $42,500",
    after: "Feeder cable allowance: $47,250",
    removed: "$42,500",
    added: "$47,250",
    evidenceImage: "/evidence/site-report-31-page-6.png",
  },
  phase: { status: "idle", busy: false },
  locked: false,
  onBack: () => undefined,
};

const queue: QueuePageProps = {
  commands: [
    { id: "q1", sequence: 1, summary: "Approve · Feeder allowance", detail: "CO-014 · Saved 9:38 AM", state: "stale" },
    { id: "q2", sequence: 2, summary: "Reject · Completion date", detail: "Waits for item 1", state: "pending" },
  ],
  conflict: {
    commandId: "q1",
    title: "Review is out of date",
    detail: "Nothing was applied",
    changes: [{ key: "Site Report 31", kind: "source_version", reviewed: "Version 2", current: "Version 3", delta: "Cost note changed from $42,500 to $45,000." }],
  },
  pendingScope: null,
  onRetry: async () => undefined,
  onReconcile: async () => undefined,
  onRefreshReview: async () => undefined,
  onLater: () => undefined,
};

const audit: AuditPageProps = {
  rows: [{ id: "audit-1", eventType: "Decision applied", actor: "Maya Chen", source: "Phone via FieldLedger relay", detail: "Approved feeder allowance", correlationId: "corr-014-41", createdAt: "2026-08-23T09:48:00" }],
  pending: false,
};

const ops: OpsPageProps = {
  jobs: [
    { id: "job-1", name: "Riverside register", status: "Processing", detail: "continue_prompt" },
    { id: "job-2", name: "North Annex review", status: "Review ready", detail: "PreviewReady" },
    { id: "job-3", name: "Service Yard export", status: "Completed", detail: "Authoritative refresh complete" },
    { id: "job-4", name: "Warehouse run", status: "Failed", detail: "Provider rejected request" },
    { id: "job-5", name: "Pier 7 run", status: "Cancelled", detail: "Cancelled by operator" },
    { id: "job-6", name: "Library edit", status: "Needs reconciliation", detail: "Response was lost after dispatch" },
  ],
  pendingScope: null,
  onPoll: async () => undefined,
  onReconcile: async () => undefined,
};

export const samplePageModels = { inbox, review, queue, audit, ops };
