import { FieldLedgerClient } from "@fieldledger/api-client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { FieldLedgerDatabase } from "./database";
import {
  initializeMobile,
  refreshMobileAuthority,
  refreshStaleReview,
  type DemoSeedResponse,
} from "./remote";
import type { MobileDecisionCommand } from "./types";

const baseUrl = "https://mobile.fieldledger.test";
const projectId = "00000000-0000-4000-8000-000000000101";
const batchId = "00000000-0000-4000-8000-000000000201";

const canonicalItems = [
  {
    id: "item-1",
    title: "Approved change value",
    reason: "The signed change order supersedes the contract allowance.",
    before: "USD 100000",
    after: "USD 125000",
    status: "pending",
    evidence: { fact_key: "approved_change_value", value: 125000, unit: "USD" },
    citation: { source_name: "change-order-014.docx", coordinate: { kind: "docx", paragraph: 2 }, quoted_text: "Approved change value: USD 125000" },
  },
  {
    id: "item-2",
    title: "Revised completion date",
    reason: "The signed order establishes a new completion date.",
    before: "2027-02-28",
    after: "2027-03-15",
    status: "approved",
    evidence: { fact_key: "revised_completion_date", value: "2027-03-15" },
    citation: { source_name: "change-order-014.docx", coordinate: { kind: "docx", table: 1, row: 1, column: 1 }, quoted_text: "Revised completion date: 2027-03-15" },
  },
  {
    id: "item-3",
    title: "Installed quantity",
    reason: "The field report records installed work.",
    before: "400 m",
    after: "420 m",
    status: "rejected",
    evidence: { fact_key: "installed_quantity", value: 420, unit: "m" },
    citation: { source_name: "site-report-31.txt", coordinate: { kind: "text", line_start: 2, line_end: 2 }, quoted_text: "Installed quantity: 420 m" },
  },
  {
    id: "item-4",
    title: "Invoice amount",
    reason: "The invoice amount requires review against the signed order.",
    before: "USD 125000",
    after: "USD 122500",
    status: "pending",
    evidence: { fact_key: "invoice_amount", value: 122500, unit: "USD" },
    citation: { source_name: "invoice-1042.md", coordinate: { kind: "markdown", line_start: 2, line_end: 2 }, quoted_text: "Invoice amount: USD 122500" },
  },
  {
    id: "item-5",
    title: "Change cap",
    reason: "The revised value exceeds the configured review threshold.",
    before: "Within cap",
    after: "Human review required",
    status: "pending",
    evidence: { fact_key: "change-cap", value: "failed" },
    citation: { source_name: "rules.json", coordinate: { kind: "json", path: "$.rules.change-cap" }, quoted_text: "change-cap" },
  },
];

function json(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value), {
    status,
    headers: { "content-type": "application/json" },
  });
}

describe("authoritative mobile bootstrap and hydration", () => {
  let db: FieldLedgerDatabase;

  beforeEach(() => {
    db = new FieldLedgerDatabase(`mobile-remote-${crypto.randomUUID()}`);
  });

  afterEach(async () => {
    await db.delete();
  });

  it("gets a signed demo session, seeds once, and caches backend-shaped review data", async () => {
    const requests: Array<{ url: string; init?: RequestInit }> = [];
    let auditReads = 0;
    const fetcher: typeof fetch = async (input, init) => {
      const url = String(input);
      requests.push({ url, ...(init === undefined ? {} : { init }) });
      const path = new URL(url).pathname;
      if (path === "/api/v1/auth/demo-session") {
        expect(new Headers(init?.headers).has("X-FieldLedger-Demo-Bootstrap")).toBe(false);
        return json({
          access_token: "signed-session",
          actor_id: "00000000-0000-4000-8000-000000000301",
          global_role: "approver",
          source: "demo",
          token_type: "bearer",
        });
      }
      if (path === "/api/v1/demo/seed") {
        expect(new Headers(init?.headers).get("authorization")).toBe("Bearer signed-session");
        return json({
          project_id: projectId,
          document_id: "00000000-0000-4000-8000-000000000501",
          workflow_run_id: "00000000-0000-4000-8000-000000000401",
          review_batch_id: batchId,
          status: "ready",
          replayed: false,
        } satisfies DemoSeedResponse);
      }
      if (path === "/api/v1/projects") {
        return json({
          items: [{ id: projectId, name: "Riverside Medical Center", description: "Change order 014", status: "active" }],
          total: 1,
          limit: 50,
          offset: 0,
        });
      }
      if (path === `/api/v1/projects/${projectId}/reviews`) {
        return json({
          items: [{ id: batchId, project_id: projectId, status: "pending", dependency_digest: "sha256:reviewed" }],
          total: 1,
          limit: 50,
          offset: 0,
        });
      }
      if (path === `/api/v1/reviews/${batchId}`) {
        return json({
          id: batchId,
          project_id: projectId,
          status: "pending",
          dependency_digest: "sha256:reviewed",
          items: canonicalItems,
        });
      }
      if (path === `/api/v1/projects/${projectId}/audit`) {
        auditReads += 1;
        const seeded = {
          id: "audit-seeded",
          aggregate_id: batchId,
          aggregate_type: "review_batch",
          event_type: "demo.seeded",
          actor_id: "00000000-0000-4000-8000-000000000301",
          actor_source: "demo",
          created_at: "2026-08-24T01:00:00Z",
          payload: { correlation_id: "corr-seeded", detail: "Prepared Change order 014" },
        };
        const items = auditReads === 1 ? [seeded] : [seeded, {
          id: "audit-applied",
          aggregate_id: batchId,
          aggregate_type: "review_batch",
          event_type: "review.decisions_applied",
          actor_id: "00000000-0000-4000-8000-000000000301",
          actor_source: "mobile",
          created_at: "2026-08-24T01:02:03Z",
          payload: { correlation_id: "corr-applied", detail: "Applied mobile decision" },
        }];
        return json({ items, total: items.length, limit: 50, offset: 0 });
      }
      throw new Error(`Unhandled request: ${url}`);
    };

    const runtime = await initializeMobile(db, {
      baseUrl,
      fetcher,
      identity: "Maya Chen",
    });

    expect(runtime).toMatchObject({ projectId });
    expect(runtime.clientId).toMatch(
      /^mobile:00000000-0000-4000-8000-000000000301:[0-9a-f-]{36}$/,
    );
    expect(await db.sessions.get("active")).toMatchObject({ token: "signed-session", activeProjectId: projectId });
    expect(await db.projects.get(projectId)).toMatchObject({ name: "Riverside Medical Center", description: "Change order 014" });
    expect(await db.batches.get(batchId)).toMatchObject({
      title: "Change order 014",
      dependencyDigest: "sha256:reviewed",
      items: [
        {
          id: "item-1",
          title: "Approved change value",
          before: "USD 100000",
          after: "USD 125000",
          evidence: {
            source: "change-order-014.docx",
            coordinate: "paragraph 2",
          },
        },
        expect.any(Object),
        expect.any(Object),
        expect.any(Object),
        expect.objectContaining({ id: "item-5", title: "Change cap" }),
      ],
    });
    expect(requests.map(({ url }) => new URL(url).pathname)).toContain("/api/v1/demo/seed");

    const restarted = await initializeMobile(db, { baseUrl, fetcher, identity: "Maya Chen" });
    expect(restarted.clientId).toBe(runtime.clientId);

    await refreshMobileAuthority(db, runtime);

    expect(await db.auditRows.where("projectId").equals(projectId).toArray()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ eventType: "demo.seeded", correlationId: "corr-seeded" }),
        expect.objectContaining({
          eventType: "review.decisions_applied",
          detail: "Applied mobile decision",
          correlationId: "corr-applied",
        }),
      ]),
    );
  });

  it("uses cached signed session data when bootstrap is unavailable offline", async () => {
    await db.sessions.put({
      key: "active",
      actorId: "actor-1",
      actorName: "Maya",
      clientId: "mobile:actor-1",
      token: "cached-token",
      activeProjectId: projectId,
    });

    const runtime = await initializeMobile(db, {
      baseUrl,
      fetcher: vi.fn(async () => { throw new TypeError("offline"); }) as typeof fetch,
      identity: "Maya",
    });

    expect(runtime).toMatchObject({ projectId, clientId: "mobile:actor-1", fromCache: true });
  });

  it("replaces a stale batch from authority and supersedes its queue blocker", async () => {
    await db.projects.put({ id: projectId, name: "Riverside", description: "", status: "active" });
    await db.batches.put({
      id: batchId,
      projectId,
      projectName: "Riverside",
      title: "Old review",
      status: "stale",
      dependencyDigest: "sha256:old",
      items: [],
      cachedAt: "2026-08-23T00:00:00Z",
    });
    const command: MobileDecisionCommand = {
      schemaVersion: 1,
      id: "command-stale",
      clientId: "mobile:actor-1",
      projectId,
      sequence: 1,
      idempotencyKey: "mobile:actor-1:1",
      reviewedDigest: "sha256:old",
      payloadHash: "sha256:payload",
      payload: { batchId, decisions: [{ item_id: "item-1", decision: "approve" }] },
      state: "stale",
      createdAt: "2026-08-23T00:00:00Z",
      attempts: 1,
      lastProblem: {
        kind: "stale",
        code: "review-stale",
        title: "Review is stale",
        detail: "Dependencies changed",
        changes: [],
      },
    };
    await db.offlineCommands.put(command);
    await db.drafts.put({
      id: `${batchId}:item-1`,
      projectId,
      batchId,
      itemId: "item-1",
      decision: "approve",
      createdAt: "2026-08-23T00:00:00Z",
    });
    const client = new FieldLedgerClient(baseUrl, "session", async () => json({
      id: batchId,
      project_id: projectId,
      status: "pending",
      dependency_digest: "sha256:new",
      items: [{ id: "item-current", title: "Current review", reason: "Updated source", before: "old", after: "new", status: "pending" }],
    }));

    const replacement = await refreshStaleReview(db, client, command.id);

    expect(replacement).toMatchObject({ id: batchId, dependencyDigest: "sha256:new" });
    expect(await db.offlineCommands.get(command.id)).toMatchObject({ state: "superseded" });
    expect(await db.drafts.where("batchId").equals(batchId).count()).toBe(0);
  });
});
