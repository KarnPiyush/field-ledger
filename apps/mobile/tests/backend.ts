import { expect, type Page, type Route } from "@playwright/test";

export const projectId = "00000000-0000-4000-8000-000000000101";
export const batchId = "00000000-0000-4000-8000-000000000201";

const items = [
  ["item-1", "Approved change value", "USD 100000", "USD 125000", "change-order-014.docx", { kind: "docx", paragraph: 2 }],
  ["item-2", "Revised completion date", "2027-02-28", "2027-03-15", "change-order-014.docx", { kind: "docx", table: 1, row: 1, column: 1 }],
  ["item-3", "Installed quantity", "400 m", "420 m", "site-report-31.txt", { kind: "text", line_start: 2, line_end: 2 }],
  ["item-4", "Invoice amount", "USD 125000", "USD 122500", "invoice-1042.md", { kind: "markdown", line_start: 2, line_end: 2 }],
  ["item-5", "Change cap", "Within cap", "Human review required", "rules.json", { kind: "json", path: "$.rules.change-cap" }],
].map(([id, title, before, after, source, coordinate]) => ({
  id,
  title,
  before,
  after,
  reason: `${title} is backed by tracked project evidence.`,
  status: "pending",
  evidence: { fact_key: String(title).toLowerCase().replaceAll(" ", "_") },
  citation: { source_name: source, coordinate, quoted_text: String(after) },
}));

export interface MobileBackendState {
  bootstrapFailures: number;
  decisionOutcome: "completed" | "processing" | "stale" | "lost";
  decisionRequests: number;
  confirmationRequests: number;
  idempotencyKeys: string[];
  decisionBodies: Array<Record<string, unknown>>;
  applied: boolean;
  dependencyDigest: string;
}

async function json(route: Route, body: unknown, status = 200): Promise<void> {
  await route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

export async function installMobileBackend(
  page: Page,
  overrides: Partial<MobileBackendState> = {},
): Promise<MobileBackendState> {
  const state: MobileBackendState = {
    bootstrapFailures: 0,
    decisionOutcome: "completed",
    decisionRequests: 0,
    confirmationRequests: 0,
    idempotencyKeys: [],
    decisionBodies: [],
    applied: false,
    dependencyDigest: "sha256:reviewed",
    ...overrides,
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/v1/auth/demo-session") {
      expect(request.postDataJSON()).toEqual({ identity: "reviewer" });
      if (state.bootstrapFailures > 0) {
        state.bootstrapFailures -= 1;
        await json(route, { title: "Bootstrap unavailable" }, 503);
        return;
      }
      await json(route, {
        access_token: "signed-mobile-session",
        actor_id: "00000000-0000-4000-8000-000000000301",
        global_role: "approver",
        source: "demo",
        token_type: "bearer",
      });
      return;
    }
    if (path === "/api/v1/demo/seed") {
      expect(request.headers()["authorization"]).toBe("Bearer signed-mobile-session");
      expect(request.headers()["idempotency-key"]).toBe("fieldledger-demo-seed:v1");
      await json(route, {
        document_id: "00000000-0000-4000-8000-000000000501",
        project_id: projectId,
        workflow_run_id: "00000000-0000-4000-8000-000000000401",
        review_batch_id: batchId,
        status: "review_ready",
        replayed: false,
      });
      return;
    }
    if (path === "/api/v1/projects") {
      await json(route, {
        items: [{ id: projectId, name: "Riverside Medical Center", description: "Change order 014", status: "active" }],
        total: 1,
        limit: 50,
        offset: 0,
      });
      return;
    }
    if (path === `/api/v1/projects/${projectId}/reviews`) {
      await json(route, {
        items: [{ id: batchId, project_id: projectId, status: state.applied ? "completed" : "pending", dependency_digest: state.dependencyDigest }],
        total: 1,
        limit: 50,
        offset: 0,
      });
      return;
    }
    if (path === `/api/v1/reviews/${batchId}/confirmation`) {
      state.confirmationRequests += 1;
      await json(route, { token: "confirmation-token", dependency_digest: state.dependencyDigest, expires_at: "2026-08-25T00:00:00Z" });
      return;
    }
    if (path === `/api/v1/reviews/${batchId}/decisions`) {
      state.decisionRequests += 1;
      state.idempotencyKeys.push(request.headers()["idempotency-key"] ?? "");
      const body = request.postDataJSON() as Record<string, unknown>;
      state.decisionBodies.push(body);
      expect(body.decisions).toEqual([
        expect.objectContaining({ item_id: "item-1" }),
        expect.objectContaining({ item_id: "item-2" }),
        expect.objectContaining({ item_id: "item-3" }),
        expect.objectContaining({ item_id: "item-4" }),
        expect.objectContaining({ item_id: "item-5" }),
      ]);
      if (state.decisionOutcome === "lost") {
        await route.abort("connectionreset");
        return;
      }
      if (state.decisionOutcome === "stale") {
        state.dependencyDigest = "sha256:current";
        await json(route, {
          type: "https://fieldledger.dev/problems/stale-dependencies",
          title: "Review is out of date",
          status: 409,
          detail: "Nothing was applied because Change order 014 changed.",
          correlation_id: "corr-stale-014",
          changes: [{
            kind: "source_version",
            key: "change-order-014.docx",
            reviewed_hash: "sha256:reviewed-source",
            current_hash: "sha256:current-source",
          }],
        }, 409);
        return;
      }
      if (state.decisionOutcome === "processing") {
        await json(route, { batch_id: batchId, status: "processing", provider_job_id: "provider-job-1" });
        return;
      }
      state.applied = true;
      await json(route, { batch_id: batchId, status: "completed", replayed: false });
      return;
    }
    if (
      path === "/api/v1/ops/provider-jobs/provider-job-1/poll"
      && request.method() === "POST"
    ) {
      state.applied = true;
      await json(route, { id: "provider-job-1", review_batch_id: batchId, status: "PreviewReady" });
      return;
    }
    if (path === `/api/v1/reviews/${batchId}`) {
      await json(route, {
        id: batchId,
        project_id: projectId,
        status: state.applied ? "completed" : "pending",
        dependency_digest: state.dependencyDigest,
        items,
      });
      return;
    }
    if (path === `/api/v1/projects/${projectId}/audit`) {
      const auditItems = state.applied
        ? [{
            id: "audit-applied-014",
            aggregate_id: batchId,
            aggregate_type: "review_batch",
            event_type: "review.decisions_applied",
            actor_id: "demo-reviewer",
            actor_source: "ui",
            created_at: "2026-08-24T01:02:03Z",
            payload: {
              status: "completed",
              relay_actor_id: "demo-reviewer",
              relay_actor_source: "demo",
              idempotency_key_hash: "sha256:mocked",
              decision_ids: ["decision-1", "decision-2", "decision-3", "decision-4", "decision-5"],
            },
          }]
        : [{
            id: "audit-seeded-014",
            aggregate_id: batchId,
            aggregate_type: "review_batch",
            event_type: "demo.seeded",
            actor_id: "demo-reviewer",
            actor_source: "demo",
            created_at: "2026-08-24T00:58:00Z",
            payload: { detail: "Prepared Change order 014", correlation_id: "corr-seeded-014" },
          }];
      await json(route, { items: auditItems, total: auditItems.length, limit: 50, offset: 0 });
      return;
    }
    throw new Error(`Unhandled mobile backend route: ${request.method()} ${path}`);
  });
  return state;
}

export async function saveCanonicalMixedDecisions(page: Page): Promise<void> {
  const decisions: Array<{ kind: "approve" | "reject"; note?: string }> = [
    { kind: "approve" },
    { kind: "reject", note: "Completion evidence needs field verification." },
    { kind: "approve" },
    { kind: "approve" },
    { kind: "approve" },
  ];
  for (let index = 0; index < decisions.length; index += 1) {
    const decision = decisions[index]!;
    if (decision.kind === "reject") {
      await page.getByRole("button", { name: "Reject" }).click();
      await page.getByLabel("Reason for rejection").fill(decision.note!);
      await page.getByRole("button", { name: "Save rejection" }).click();
    } else {
      await page.getByRole("button", { name: "Approve" }).click();
    }
    await expect(page.getByText(/Saved on this phone|Batch saved in the ordered queue/)).toBeVisible();
    if (index + 1 < decisions.length) {
      await page.getByRole("button", { name: "Next" }).click();
      await expect(page.getByLabel(`Item ${index + 2} of 5`)).toBeVisible();
    }
  }
}
