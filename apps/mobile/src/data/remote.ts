import {
  FieldLedgerApiError,
  FieldLedgerClient,
  type components,
} from "@fieldledger/api-client";
import { transitionCommand } from "@fieldledger/offline-queue";

import type { FieldLedgerDatabase } from "./database";
import type {
  AuditRecord,
  ProjectRecord,
  ReviewBatchRecord,
  SessionRecord,
} from "./types";

export type DemoSeedResponse = components["schemas"]["DemoSeedResponse"];

export interface MobileRuntime {
  client: FieldLedgerClient;
  projectId: string;
  clientId: string;
  workflowRunId?: string;
  fromCache: boolean;
}

interface SeedContext {
  client: FieldLedgerClient;
}

interface InitializeOptions {
  baseUrl: string;
  fetcher: typeof fetch;
  identity: string;
  seedDemo?: (context: SeedContext) => Promise<DemoSeedResponse>;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

async function requestJson(
  fetcher: typeof fetch,
  url: URL,
  init: RequestInit,
): Promise<unknown> {
  const response = await fetcher(url, init);
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text.length === 0 ? undefined : JSON.parse(text);
  } catch {
    payload = undefined;
  }
  if (!response.ok) {
    throw new FieldLedgerApiError(
      response.status,
      isRecord(payload) ? payload : undefined,
    );
  }
  return payload;
}

function isDemoSession(value: unknown): value is components["schemas"]["DemoSessionResponse"] {
  return (
    isRecord(value) &&
    typeof value.access_token === "string" &&
    typeof value.actor_id === "string" &&
    typeof value.global_role === "string" &&
    typeof value.source === "string" &&
    typeof value.token_type === "string"
  );
}

function isSeed(value: unknown): value is DemoSeedResponse {
  return (
    isRecord(value) &&
    typeof value.project_id === "string" &&
    typeof value.workflow_run_id === "string" &&
    typeof value.review_batch_id === "string" &&
    typeof value.status === "string"
  );
}

async function bootstrapSession(options: InitializeOptions) {
  const payload = await requestJson(
    options.fetcher,
    new URL("/api/v1/auth/demo-session", options.baseUrl),
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ identity: options.identity }),
    },
  );
  if (!isDemoSession(payload)) throw new TypeError("Demo session response is invalid");
  return payload;
}

async function defaultSeedDemo({ client }: SeedContext) {
  const payload = await client.request("seed_demo", {
    path: {},
    headers: { "Idempotency-Key": "fieldledger-demo-seed:v1" },
    body: {},
  });
  if (!isSeed(payload)) throw new TypeError("Demo seed response is invalid");
  return payload;
}

function projectRecord(
  value: components["schemas"]["ProjectResponse"],
): ProjectRecord {
  return {
    id: value.id,
    name: value.name,
    description: value.description ?? "",
    status: value.status,
  };
}

export function toReviewBatchRecord(
  value: components["schemas"]["ReviewBatchResponse"],
  projectName: string,
  batchTitle?: string,
  cachedAt = new Date().toISOString(),
): ReviewBatchRecord {
  return {
    id: value.id,
    projectId: value.project_id,
    projectName,
    title: batchTitle || value.items[0]?.title || "FieldLedger review",
    status: value.status === "completed" || value.status === "applied" ? "synced" : "pending",
    dependencyDigest: value.dependency_digest ?? "",
    cachedAt,
    items: value.items.map((item) => {
      const evidence = isRecord(item.evidence) ? item.evidence : {};
      const citation = isRecord(item.citation) ? item.citation : {};
      const factKey = stringField(evidence, "fact_key");
      return {
        id: item.id,
        category: factKey ? factKey.replaceAll("_", " ") : "Review item",
        title: item.title,
        summary: item.reason,
        reason: item.reason,
        before: item.before,
        after: item.after,
        removed: item.before,
        added: item.after,
        status: item.status,
        evidence: {
          source:
            stringField(evidence, "source") ??
            stringField(evidence, "source_name") ??
            stringField(citation, "source_name") ??
            "Tracked project source",
          coordinate: coordinateLabel(citation.coordinate ?? evidence.coordinate),
          image: stringField(evidence, "image") ?? "/evidence/site-report-31-page-6.png",
        },
      };
    }),
  };
}

function stringField(value: Record<string, unknown>, key: string): string | undefined {
  return typeof value[key] === "string" ? value[key] : undefined;
}

function coordinateLabel(value: unknown): string {
  if (typeof value === "string" && value.length > 0) return value;
  if (!isRecord(value)) return "Authoritative review evidence";
  const parts = Object.entries(value)
    .filter(([key, field]) => key !== "kind" && (typeof field === "string" || typeof field === "number"))
    .map(([key, field]) => `${key.replaceAll("_", " ")} ${String(field)}`);
  return parts.length > 0 ? parts.join(" · ") : "Authoritative review evidence";
}

function auditRecord(
  value: components["schemas"]["AuditEventResponse"],
  projectId: string,
  index: number,
): AuditRecord {
  const payload = value.payload ?? {};
  return {
    id: value.id ?? `${value.event_type}:${value.aggregate_id ?? "project"}:${value.created_at ?? index}`,
    projectId,
    eventType: value.event_type,
    actor: value.actor_id ?? "FieldLedger",
    source: value.actor_source ?? "FieldLedger relay",
    detail: typeof payload.detail === "string" ? payload.detail : value.event_type,
    correlationId:
      typeof payload.correlation_id === "string" ? payload.correlation_id : "not-recorded",
    createdAt: value.created_at ?? new Date().toISOString(),
  };
}

async function hydrate(
  db: FieldLedgerDatabase,
  client: FieldLedgerClient,
  session: SessionRecord,
): Promise<void> {
  const projects = await client.request("list_projects_api_v1_projects_get", {
    path: {},
    query: { limit: 50, offset: 0 },
    headers: {},
  });
  const project = projects.items.find((item) => item.id === session.activeProjectId);
  if (!project) throw new Error("Seeded project is not visible to this session");
  const [reviewList, audit] = await Promise.all([
    client.request("list_reviews_api_v1_projects__project_id__reviews_get", {
      path: { project_id: project.id },
      query: { limit: 50, offset: 0 },
    }),
    client.request("list_audit_api_v1_projects__project_id__audit_get", {
      path: { project_id: project.id },
      query: { limit: 50, offset: 0 },
    }),
  ]);
  const batchDtos = await Promise.all(
    reviewList.items.map((summary) =>
      client.request("get_review_api_v1_reviews__batch_id__get", {
        path: { batch_id: summary.id },
      }),
    ),
  );
  const cachedAt = new Date().toISOString();
  const batches = batchDtos.map((batch) =>
    toReviewBatchRecord(batch, project.name, project.description ?? undefined, cachedAt),
  );
  const auditRows = audit.items.map((item, index) => auditRecord(item, project.id, index));

  await db.transaction(
    "rw",
    [db.sessions, db.projects, db.batches, db.auditRows, db.syncMetadata],
    async () => {
      await db.sessions.put(session);
      await db.projects.put(projectRecord(project));
      await db.batches.where("projectId").equals(project.id).delete();
      await db.auditRows.where("projectId").equals(project.id).delete();
      if (batches.length > 0) await db.batches.bulkPut(batches);
      if (auditRows.length > 0) await db.auditRows.bulkPut(auditRows);
      await db.syncMetadata.put({
        projectId: project.id,
        cachedAt,
        lastAttemptAt: cachedAt,
        connection: "online",
      });
    },
  );
}

export async function initializeMobile(
  db: FieldLedgerDatabase,
  options: InitializeOptions,
): Promise<MobileRuntime> {
  const cached = await db.sessions.get("active");
  try {
    const signed = await bootstrapSession(options);
    const client = new FieldLedgerClient(options.baseUrl, signed.access_token, options.fetcher);
    const seed = await (options.seedDemo ?? defaultSeedDemo)({ client });
    const session: SessionRecord = {
      key: "active",
      actorId: signed.actor_id,
      actorName: options.identity,
      clientId:
        cached?.actorId === signed.actor_id
          ? cached.clientId
          : `mobile:${signed.actor_id}:${crypto.randomUUID()}`,
      token: signed.access_token,
      activeProjectId: seed.project_id,
    };
    await hydrate(db, client, session);
    return {
      client,
      projectId: seed.project_id,
      clientId: session.clientId,
      workflowRunId: seed.workflow_run_id,
      fromCache: false,
    };
  } catch (error) {
    if (!cached) throw error;
    return {
      client: new FieldLedgerClient(options.baseUrl, cached.token, options.fetcher),
      projectId: cached.activeProjectId,
      clientId: cached.clientId,
      fromCache: true,
    };
  }
}

export async function refreshMobileAuthority(
  db: FieldLedgerDatabase,
  runtime: MobileRuntime,
): Promise<void> {
  const session = await db.sessions.get("active");
  if (!session || session.activeProjectId !== runtime.projectId) {
    throw new Error("Active mobile session is unavailable");
  }
  await hydrate(db, runtime.client, session);
}

export async function refreshStaleReview(
  db: FieldLedgerDatabase,
  client: FieldLedgerClient,
  commandId: string,
): Promise<ReviewBatchRecord> {
  const command = await db.offlineCommands.get(commandId);
  if (!command || command.state !== "stale") throw new Error("Stale command was not found");
  const project = await db.projects.get(command.projectId);
  if (!project) throw new Error("Project was not found");
  const response = await client.request("get_review_api_v1_reviews__batch_id__get", {
    path: { batch_id: command.payload.batchId },
  });
  const replacement = toReviewBatchRecord(response, project.name, project.description);
  const superseded = transitionCommand(command, { kind: "supersede_stale" });
  await db.transaction("rw", [db.batches, db.drafts, db.offlineCommands], async () => {
    const current = await db.offlineCommands.get(commandId);
    if (current?.state !== "stale") throw new Error("Stale command changed during refresh");
    await db.batches.put(replacement);
    await db.drafts.where("batchId").equals(command.payload.batchId).delete();
    await db.offlineCommands.put(superseded);
  });
  return replacement;
}
