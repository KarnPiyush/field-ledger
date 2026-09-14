import { drainInOrder, QueueSerializationError, reconcileBlockedCommand, transitionCommand, type DrainSummary } from "@fieldledger/offline-queue";
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from "react";
import { Navigate, Route, Routes, useLocation, useNavigate, useParams } from "react-router-dom";

import { MobileQueueSender } from "../api/queueSender";
import { AppBar } from "../components/AppBar";
import { ConnectionStrip } from "../components/ConnectionStrip";
import { Navigation } from "../components/Navigation";
import { FieldLedgerDatabase, fieldLedgerDb } from "../data/database";
import { registerQueueLifecycle, type QueueLifecycle } from "../data/lifecycle";
import { DecisionStore, DexieQueueStore } from "../data/queueStore";
import {
  initializeMobile,
  refreshMobileAuthority,
  refreshStaleReview,
  type MobileRuntime,
} from "../data/remote";
import type {
  AuditRecord,
  DraftRecord,
  MobileDecisionCommand,
  ProviderJobRecord,
  ReviewBatchRecord,
  SaveDecisionInput,
} from "../data/types";
import { AuditPage } from "../pages/AuditPage";
import { InboxPage } from "../pages/InboxPage";
import { OpsPage } from "../pages/OpsPage";
import { QueuePage } from "../pages/QueuePage";
import { ReviewPage } from "../pages/ReviewPage";
import { asyncPhaseReducer, createAsyncPhases, phaseFor, type AsyncOutcome } from "../state/asyncPhase";

interface Snapshot {
  batches: ReviewBatchRecord[];
  commands: MobileDecisionCommand[];
  audit: AuditRecord[];
  jobs: ProviderJobRecord[];
  drafts: DraftRecord[];
  cachedAt: string;
}

type Initializer = (db: FieldLedgerDatabase) => Promise<MobileRuntime>;

export interface AppProps {
  db?: FieldLedgerDatabase;
  initialize?: Initializer;
}

const EMPTY: Snapshot = {
  batches: [],
  commands: [],
  audit: [],
  jobs: [],
  drafts: [],
  cachedAt: new Date().toISOString(),
};

function apiBaseUrl(): string {
  const configured = import.meta.env.VITE_API_BASE_URL;
  if (!configured) throw new Error("VITE_API_BASE_URL is required");
  return new URL(configured, window.location.origin).toString();
}

const defaultInitializer: Initializer = (db) =>
  initializeMobile(db, {
    baseUrl: apiBaseUrl(),
    fetcher: window.fetch.bind(window),
    identity: import.meta.env.VITE_DEMO_IDENTITY ?? "reviewer",
  });

function jobLabel(status: string): string {
  const normalized = status.toLowerCase().replaceAll("_", "");
  if (normalized === "processing" || normalized === "continueprompt") return "Processing";
  if (normalized === "reviewready" || normalized === "previewready" || normalized === "awaitingapproval") return "Review ready";
  if (normalized === "completed") return "Completed";
  if (normalized === "failed") return "Failed";
  if (normalized === "cancelled") return "Cancelled";
  return "Needs reconciliation";
}

function commandSummary(command: MobileDecisionCommand, batches: ReviewBatchRecord[]): string {
  const decision = command.payload.decisions[0];
  const item = batches
    .flatMap((batch) => batch.items)
    .find((candidate) => candidate.id === decision?.item_id);
  if (command.payload.decisions.length > 1) {
    const batch = batches.find((candidate) => candidate.id === command.payload.batchId);
    return `${command.payload.decisions.length} decisions · ${batch?.title ?? "Review batch"}`;
  }
  const action = decision?.decision === "reject" ? "Reject" : "Approve";
  return `${action} · ${item?.title ?? "Review decision"}`;
}

function errorText(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

function drainOutcome(summary: DrainSummary): AsyncOutcome {
  if (summary.status === "drained") return "completed";
  if (summary.status === "reconciliation_required") return "reconciliation_required";
  if (summary.status === "contended") return "unknown";
  return summary.status;
}

export function App({ db = fieldLedgerDb, initialize = defaultInitializer }: AppProps) {
  const [snapshot, setSnapshot] = useState(EMPTY);
  const [runtime, setRuntime] = useState<MobileRuntime | null>(null);
  const [loading, setLoading] = useState(true);
  const [startupError, setStartupError] = useState<string>();
  const [queueCorrupt, setQueueCorrupt] = useState(false);
  const [initializationAttempt, setInitializationAttempt] = useState(0);
  const [online, setOnline] = useState(navigator.onLine);
  const [syncing, setSyncing] = useState(false);
  const [phases, dispatch] = useReducer(asyncPhaseReducer, undefined, createAsyncPhases);
  const lifecycleRef = useRef<QueueLifecycle | null>(null);
  const location = useLocation();
  const navigate = useNavigate();

  const queueStore = useMemo(() => new DexieQueueStore(db), [db]);
  const sender = useMemo(
    () => (runtime === null ? null : new MobileQueueSender(runtime.client, db, runtime.clientId)),
    [runtime, db],
  );
  const decisions = useMemo(
    () => (runtime === null ? null : new DecisionStore(db, runtime.clientId)),
    [db, runtime],
  );

  const loadSnapshot = useCallback(async (projectId: string) => {
    let batches: ReviewBatchRecord[];
    let allCommands;
    let audit: AuditRecord[];
    let jobs: ProviderJobRecord[];
    let drafts: DraftRecord[];
    let metadata;
    try {
      [batches, allCommands, audit, jobs, drafts, metadata] = await Promise.all([
        db.batches.where("projectId").equals(projectId).toArray(),
        queueStore.list(),
        db.auditRows.where("projectId").equals(projectId).toArray(),
        db.providerJobs.where("projectId").equals(projectId).toArray(),
        db.drafts.where("projectId").equals(projectId).toArray(),
        db.syncMetadata.get(projectId),
      ]);
      setQueueCorrupt(false);
    } catch (error) {
      if (error instanceof QueueSerializationError) setQueueCorrupt(true);
      throw error;
    }
    const commands = allCommands.filter((command) => command.projectId === projectId);
    const order: Record<string, number> = { pending: 0, stale: 1, synced: 2 };
    batches.sort((left, right) => (order[left.status] ?? 9) - (order[right.status] ?? 9));
    commands.sort((left, right) => left.sequence - right.sequence);
    audit.sort((left, right) => right.createdAt.localeCompare(left.createdAt));
    jobs.sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    setSnapshot({
      batches,
      commands: commands as MobileDecisionCommand[],
      audit,
      jobs,
      drafts,
      cachedAt: metadata?.cachedAt ?? new Date().toISOString(),
    });
  }, [db, queueStore]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      setLoading(true);
      setStartupError(undefined);
      try {
        const initialized = await initialize(db);
        if (cancelled) return;
        setRuntime(initialized);
        await loadSnapshot(initialized.projectId);
        if (initialized.fromCache) {
          setStartupError("Showing saved data. FieldLedger could not refresh while offline.");
        }
      } catch (error) {
        const correlationId = `startup-${crypto.randomUUID()}`;
        console.error("Mobile startup failed", { correlationId, error: errorText(error) });
        if (!cancelled) {
          setStartupError(
            error instanceof QueueSerializationError
              ? "The saved queue on this phone cannot be read."
              : "Could not load FieldLedger data. Check the connection and retry.",
          );
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [db, initializationAttempt, initialize, loadSnapshot]);

  const drainQueue = useCallback(async (): Promise<DrainSummary | null> => {
    if (runtime === null || sender === null) return null;
    dispatch({ type: "begin", scope: "queue:drain" });
    setSyncing(true);
    try {
      const summary = await drainInOrder(queueStore, sender);
      if (summary.status !== "offline") await refreshMobileAuthority(db, runtime);
      await loadSnapshot(runtime.projectId);
      dispatch({
        type: "finish",
        scope: "queue:drain",
        outcome: drainOutcome(summary),
        ...(summary.status === "drained" ? {} : { message: "Queue stopped for recovery." }),
      });
      return summary;
    } catch (error) {
      const correlationId = `drain-${crypto.randomUUID()}`;
      console.error("Queue drain failed", { correlationId, error: errorText(error) });
      dispatch({
        type: "finish",
        scope: "queue:drain",
        outcome: "fetch_error",
        message: "Queue sync failed. Retry remains available.",
        correlationId,
      });
      return null;
    } finally {
      setSyncing(false);
    }
  }, [db, loadSnapshot, queueStore, runtime, sender]);

  useEffect(() => {
    if (runtime === null || sender === null || queueCorrupt) return;
    const lifecycle = registerQueueLifecycle(drainQueue);
    lifecycleRef.current = lifecycle;
    return () => {
      lifecycle.dispose();
      if (lifecycleRef.current === lifecycle) lifecycleRef.current = null;
    };
  }, [drainQueue, queueCorrupt, runtime, sender]);

  useEffect(() => {
    const onOnline = () => setOnline(true);
    const onOffline = () => setOnline(false);
    window.addEventListener("online", onOnline);
    window.addEventListener("offline", onOffline);
    return () => {
      window.removeEventListener("online", onOnline);
      window.removeEventListener("offline", onOffline);
    };
  }, []);

  const finishActionError = (scope: string, message: string, error: unknown) => {
    const correlationId = `mobile-${crypto.randomUUID()}`;
    console.error("Mobile action failed", { scope, correlationId, error: errorText(error) });
    dispatch({ type: "finish", scope, outcome: "fetch_error", message, correlationId });
  };

  const saveDecision = async (input: SaveDecisionInput) => {
    const scope = `decision:${input.itemId}`;
    if (phaseFor(phases, scope).busy || decisions === null || runtime === null) return;
    dispatch({ type: "begin", scope });
    try {
      await decisions.save(input);
      await loadSnapshot(runtime.projectId);
      dispatch({ type: "finish", scope, outcome: "applied", message: "Saved on this phone" });
    } catch (error) {
      finishActionError(scope, "Could not save on this phone", error);
      throw error;
    }
  };

  const retryCommand = async (commandId: string) => {
    const scope = `queue:${commandId}`;
    if (phaseFor(phases, scope).busy || runtime === null) return;
    dispatch({ type: "begin", scope });
    try {
      const command = await db.offlineCommands.get(commandId);
      if (!command || command.state !== "failed") throw new Error("Failed command was not found");
      const pending = transitionCommand(command, { kind: "retry_failed" });
      if (!(await queueStore.compareAndSet(commandId, "failed", pending))) {
        throw new Error("Command changed before retry");
      }
      const summary = await drainQueue();
      dispatch({
        type: "finish",
        scope,
        outcome: summary === null ? "fetch_error" : drainOutcome(summary),
      });
    } catch (error) {
      finishActionError(scope, "Retry failed. The command remains available.", error);
    }
  };

  const reconcileCommand = async (commandId: string) => {
    const scope = `queue:${commandId}`;
    if (phaseFor(phases, scope).busy || runtime === null || sender === null) return;
    dispatch({ type: "begin", scope });
    try {
      const command = await db.offlineCommands.get(commandId);
      if (!command || command.state !== "reconciliation_required") {
        throw new Error("Reconciliation command was not found");
      }
      const result = await reconcileBlockedCommand(queueStore, sender, command);
      if (result.kind === "applied") {
        dispatch({ type: "finish", scope, outcome: "completed" });
      } else {
        dispatch({ type: "finish", scope, outcome: result.kind, message: result.problem.detail });
      }
      await loadSnapshot(runtime.projectId);
    } catch (error) {
      finishActionError(scope, "Reconciliation failed. Retry remains available.", error);
    }
  };

  const refreshForReview = async (commandId: string) => {
    const scope = `queue:${commandId}`;
    if (phaseFor(phases, scope).busy || runtime === null) return;
    dispatch({ type: "begin", scope });
    try {
      const replacement = await refreshStaleReview(db, runtime.client, commandId);
      await loadSnapshot(runtime.projectId);
      dispatch({ type: "finish", scope, outcome: "review_ready", message: "Authoritative review refreshed" });
      navigate(`/review/${encodeURIComponent(replacement.id)}/1`);
    } catch (error) {
      finishActionError(scope, "Could not refresh this review. Nothing was applied.", error);
    }
  };

  const pollJob = async (jobId: string) => {
    const scope = `job:${jobId}`;
    if (phaseFor(phases, scope).busy || runtime === null) return;
    dispatch({ type: "begin", scope });
    try {
      const response = await runtime.client.request(
        "poll_provider_job_api_v1_ops_provider_jobs__job_id__poll_post",
        { path: { job_id: jobId }, body: {} },
      );
      const current = await db.providerJobs.get(jobId);
      if (current) {
        await db.providerJobs.put({ ...current, status: response.status, detail: response.status, updatedAt: new Date().toISOString() });
      }
      const normalized = jobLabel(response.status);
      const outcome: AsyncOutcome =
        normalized === "Review ready"
          ? "review_ready"
          : normalized === "Completed"
            ? "completed"
            : normalized === "Failed"
              ? "failed"
              : normalized === "Cancelled"
                ? "cancelled"
                : normalized === "Needs reconciliation"
                  ? "reconciliation_required"
                  : "continue_prompt";
      dispatch({ type: "finish", scope, outcome, message: normalized });
      await loadSnapshot(runtime.projectId);
    } catch (error) {
      const correlationId = `poll-${crypto.randomUUID()}`;
      console.error("Provider poll failed", { correlationId, error: errorText(error) });
      try {
        const current = await db.providerJobs.get(jobId);
        if (current) {
          await db.providerJobs.put({
            ...current,
            status: "reconciliation_required",
            detail: "Poll response could not be confirmed",
            updatedAt: new Date().toISOString(),
          });
        }
        await loadSnapshot(runtime.projectId);
      } catch (readError) {
        console.error("Provider poll recovery read failed", { correlationId, error: errorText(readError) });
      } finally {
        dispatch({ type: "finish", scope, outcome: "lost_response", message: "Needs reconciliation", correlationId });
      }
    }
  };

  const exportCorruptQueue = async () => {
    try {
      const commands = await db.offlineCommands.toArray();
      const href = URL.createObjectURL(
        new Blob([JSON.stringify({ schemaVersion: 1, commands }, null, 2)], {
          type: "application/json",
        }),
      );
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = "fieldledger-mobile-queue.json";
      anchor.click();
      URL.revokeObjectURL(href);
    } catch (error) {
      finishActionError("queue:export", "The saved queue could not be exported.", error);
    }
  };

  const resetCorruptQueue = async () => {
    if (runtime === null) return;
    setLoading(true);
    try {
      await db.transaction("rw", [db.offlineCommands, db.submissions], async () => {
        await db.offlineCommands.clear();
        await db.submissions.clear();
      });
      setQueueCorrupt(false);
      setStartupError(undefined);
      await loadSnapshot(runtime.projectId);
    } catch (error) {
      const message = "The saved queue could not be reset.";
      setStartupError(message);
      finishActionError("queue:reset", message, error);
    } finally {
      setLoading(false);
    }
  };

  const reviewMatch = location.pathname.match(/^\/review\/([^/]+)\/(\d+)$/);
  const reviewBatch = reviewMatch
    ? snapshot.batches.find((batch) => batch.id === decodeURIComponent(reviewMatch[1] ?? ""))
    : undefined;
  const reviewIndex = reviewMatch ? Number(reviewMatch[2]) - 1 : -1;
  const queuedCount = snapshot.commands.filter(
    (command) => command.state !== "applied" && command.state !== "superseded",
  ).length;
  const connectionState = online
    ? phaseFor(phases, "queue:drain").status === "error"
      ? "error"
      : "online"
    : "offline";

  return (
    <div className="app-shell">
      <div className="top-stack">
        {reviewBatch ? (
          <AppBar title={reviewBatch.title.split(" · ")[0] ?? reviewBatch.title} subtitle={reviewBatch.projectName} progress={`${reviewIndex + 1} of ${reviewBatch.items.length}`} onBack={() => navigate("/inbox")} />
        ) : (
          <AppBar refreshing={syncing} onRefresh={() => void lifecycleRef.current?.retry()} />
        )}
        <ConnectionStrip state={syncing ? "syncing" : connectionState} queued={queuedCount} {...(phaseFor(phases, "queue:drain").message ? { message: phaseFor(phases, "queue:drain").message } : {})} />
      </div>
      <Navigation />
      <main id="main-content" className={reviewBatch ? "main-content main-content--review" : "main-content"}>
        {loading ? (
          <div className="app-loading" role="status">Loading saved field work…</div>
        ) : (
          <Routes>
            <Route path="/" element={<Navigate to="/inbox" replace />} />
            <Route path="/inbox" element={<InboxPage metrics={{ pending: snapshot.batches.filter((batch) => batch.status === "pending").length, queued: queuedCount, stale: snapshot.commands.filter((command) => command.state === "stale").length }} cachedAt={snapshot.cachedAt} batches={snapshot.batches.map((batch) => ({ id: batch.id, projectName: batch.projectName, title: batch.title, detail: batch.status === "synced" ? "Decisions synchronized" : "Ready for review", count: batch.items.length, status: batch.status === "synced" ? "synced" : "pending" }))} connection={online ? "online" : "offline"} pending={false} {...(startupError ? { error: startupError } : {})} {...(queueCorrupt ? { onExportQueue: exportCorruptQueue, onResetQueue: resetCorruptQueue } : {})} onRefresh={async () => { if (lifecycleRef.current) await lifecycleRef.current.retry(); else setInitializationAttempt((value) => value + 1); }} />} />
            <Route path="/review/:batchId/:itemNumber" element={<ReviewRoute batches={snapshot.batches} commands={snapshot.commands} drafts={snapshot.drafts} phases={phases} onSave={saveDecision} />} />
            <Route path="/queue" element={<QueuePage commands={snapshot.commands.map((command) => ({ id: command.id, sequence: command.sequence, summary: commandSummary(command, snapshot.batches), detail: `${command.payload.batchId} · Saved ${new Date(command.createdAt).toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`, state: command.state }))} {...(snapshot.commands.find((command) => command.state === "stale" && command.lastProblem?.kind === "stale")?.lastProblem?.kind === "stale" ? { conflict: (() => { const command = snapshot.commands.find((candidate) => candidate.state === "stale" && candidate.lastProblem?.kind === "stale")!; const problem = command.lastProblem!; return { commandId: command.id, title: problem.title, detail: problem.detail, changes: problem.kind === "stale" ? problem.changes.map((change) => ({ key: change.key, kind: change.kind, reviewed: change.reviewed_hash ?? "Not present", current: change.current_hash ?? "Removed", delta: `${change.kind} changed on the server.` })) : [] }; })() } : {})} pendingScope={snapshot.commands.find((command) => phaseFor(phases, `queue:${command.id}`).busy)?.id ?? null} onRetry={retryCommand} onReconcile={reconcileCommand} onRefreshReview={refreshForReview} onLater={() => navigate("/inbox")} />} />
            <Route path="/audit" element={<AuditPage rows={snapshot.audit} pending={false} />} />
            <Route path="/ops" element={<OpsPage jobs={snapshot.jobs.map((job) => ({ id: job.id, name: job.name, status: jobLabel(job.status), detail: job.detail }))} pendingScope={snapshot.jobs.find((job) => phaseFor(phases, `job:${job.id}`).busy)?.id ?? null} onPoll={pollJob} onReconcile={pollJob} />} />
            <Route path="*" element={<Navigate to="/inbox" replace />} />
          </Routes>
        )}
      </main>
    </div>
  );
}

function ReviewRoute({ batches, commands, drafts, phases, onSave }: { batches: ReviewBatchRecord[]; commands: MobileDecisionCommand[]; drafts: DraftRecord[]; phases: ReturnType<typeof createAsyncPhases>; onSave: (input: SaveDecisionInput) => Promise<void> }) {
  const { batchId = "", itemNumber = "1" } = useParams();
  const navigate = useNavigate();
  const batch = batches.find((candidate) => candidate.id === batchId);
  const index = Math.max(0, Number(itemNumber) - 1);
  const item = batch?.items[index];
  if (!batch || !item) return <Navigate to="/inbox" replace />;
  const draft = drafts.find((candidate) => candidate.batchId === batch.id && candidate.itemId === item.id);
  const locked = commands.some(
    (command) =>
      command.payload.batchId === batch.id &&
      command.state !== "superseded",
  );
  return <ReviewPage projectName={batch.projectName} batchTitle={batch.title} progress={{ current: index + 1, total: batch.items.length }} item={{ id: item.id, category: item.category, title: item.title, summary: item.summary, reason: item.reason, source: item.evidence.source, coordinate: item.evidence.coordinate, before: item.before, after: item.after, removed: item.removed, added: item.added, evidenceImage: item.evidence.image }} phase={phaseFor(phases, `decision:${item.id}`)} locked={locked} {...(draft ? { draft: { decision: draft.decision, ...(draft.note ? { note: draft.note } : {}) } } : {})} onBack={() => navigate("/inbox")} {...(index > 0 ? { onPrevious: () => navigate(`/review/${encodeURIComponent(batch.id)}/${index}`) } : {})} {...(index + 1 < batch.items.length ? { onNext: () => navigate(`/review/${encodeURIComponent(batch.id)}/${index + 2}`) } : {})} onDecision={(decision, note) => onSave({ projectId: batch.projectId, batchId: batch.id, itemId: item.id, batchItemIds: batch.items.map((candidate) => candidate.id), decision, ...(note ? { note } : {}), reviewedDigest: batch.dependencyDigest, createdAt: new Date().toISOString() })} />;
}
