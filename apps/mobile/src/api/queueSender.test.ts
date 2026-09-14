import { FieldLedgerClient } from "@fieldledger/api-client";
import { drainInOrder, reconcileBlockedCommand } from "@fieldledger/offline-queue";
import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { MobileQueueSender } from "./queueSender";
import type { MobileDecisionCommand } from "../data/types";
import { FieldLedgerDatabase } from "../data/database";
import { DexieQueueStore } from "../data/queueStore";
import { SubmissionStore } from "../data/submissions";

const api = "http://fieldledger.test";
const command: MobileDecisionCommand = {
  schemaVersion: 1,
  id: "command-1",
  clientId: "phone-1",
  projectId: "project-1",
  sequence: 1,
  idempotencyKey: "phone-1:1",
  reviewedDigest: "digest-1",
  payloadHash: "payload-1",
  payload: { batchId: "batch-1", decisions: [{ item_id: "item-1", decision: "approve" }] },
  state: "sending",
  createdAt: "2026-08-23T09:38:00.000Z",
  attempts: 1,
};

const server = setupServer();
let db: FieldLedgerDatabase;

function sender(options: { online?: boolean; maxPolls?: number } = {}) {
  return new MobileQueueSender(new FieldLedgerClient(api, "test-session"), db, "phone-1", {
    isOnline: () => options.online ?? true,
    maxPolls: options.maxPolls ?? 2,
    wait: async () => undefined,
    log: vi.fn(),
  });
}

function confirmation() {
  return http.post(`${api}/api/v1/reviews/batch-1/confirmation`, () =>
    HttpResponse.json({ token: "confirm-1", dependency_digest: "digest-1", expires_at: "2026-08-23T10:38:00Z" }),
  );
}

function currentReview() {
  return http.get(`${api}/api/v1/reviews/batch-1`, () =>
    HttpResponse.json({ id: "batch-1", project_id: "project-1", status: "applied", dependency_digest: "digest-1", items: [] }),
  );
}

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
beforeEach(async () => {
  db = new FieldLedgerDatabase(`sender-${crypto.randomUUID()}`);
  await new DexieQueueStore(db).allocateAndAppend({
    id: command.id, clientId: command.clientId, projectId: command.projectId, deduplicationKey: "batch-1:decisions",
    payloadHash: command.payloadHash, payload: command.payload,
    reviewedDigest: command.reviewedDigest, createdAt: command.createdAt,
  });
});
afterEach(async () => {
  server.resetHandlers();
  await db.delete();
});
afterAll(() => server.close());

describe("mobile queue sender", () => {
  it("replays the original confirmation and body after a lost response with a new sender", async () => {
    let confirmations = 0;
    const requests: { key: string | null; body: unknown }[] = [];
    server.use(
      http.post(`${api}/api/v1/reviews/batch-1/confirmation`, () =>
        HttpResponse.json({ token: `confirm-${++confirmations}`, dependency_digest: "digest-1" }),
      ),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, async ({ request }) => {
        requests.push({ key: request.headers.get("idempotency-key"), body: await request.json() });
        if (requests.length === 1) return HttpResponse.error();
        if (JSON.stringify(requests[0]) !== JSON.stringify(requests[1])) {
          return HttpResponse.json({ type: "https://fieldledger.local/problems/idempotency-conflict" }, { status: 409 });
        }
        return HttpResponse.json({ batch_id: "batch-1", status: "completed", replayed: true });
      }),
      currentReview(),
    );
    await expect(drainInOrder(new DexieQueueStore(db), sender())).resolves.toMatchObject({ status: "reconciliation_required" });
    const databaseName = db.name;
    db.close();
    db = new FieldLedgerDatabase(databaseName);
    const store = new DexieQueueStore(db);
    const blocked = (await store.list())[0]!;
    expect(blocked.state).toBe("reconciliation_required");
    await expect(reconcileBlockedCommand(store, sender(), blocked)).resolves.toMatchObject({ kind: "applied" });
    expect((await store.list())[0]!.state).toBe("applied");
    expect(confirmations).toBe(1);
    expect(requests).toHaveLength(2);
    expect(requests[1]).toEqual(requests[0]);
  });

  it("sends the generated API contract with the immutable idempotency key and refreshes authority", async () => {
    let requestBody: unknown;
    let idempotencyKey: string | null = null;
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, async ({ request }) => {
        requestBody = await request.json();
        idempotencyKey = request.headers.get("idempotency-key");
        return HttpResponse.json({ batch_id: "batch-1", status: "completed" });
      }),
      currentReview(),
    );

    await expect(sender().send(command)).resolves.toMatchObject({ kind: "applied" });
    expect(idempotencyKey).toBe("phone-1:1");
    expect(requestBody).toEqual({
      confirmation_token: "confirm-1",
      decisions: [{ item_id: "item-1", decision: "approve" }],
    });
  });

  it("does not make a request when the browser is definitely offline", async () => {
    const request = vi.fn();
    server.events.on("request:start", request);

    await expect(sender({ online: false }).send(command)).resolves.toMatchObject({
      kind: "offline",
      problem: { code: "offline", title: "Waiting to sync" },
    });
    expect(request).not.toHaveBeenCalled();
  });

  it("keeps exact stale dependency changes reviewable", async () => {
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () =>
        HttpResponse.json(
          {
            type: "https://fieldledger.local/problems/review-stale",
            title: "Review is stale",
            status: 409,
            detail: "Review dependencies changed after confirmation.",
            instance: "/api/v1/reviews/batch-1/decisions",
            correlation_id: "corr-stale",
            changes: [
              {
                key: "source:site-report-31",
                kind: "source_version",
                reviewed_hash: "v2",
                current_hash: "v3",
              },
            ],
          },
          { status: 409, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );

    await expect(sender().send(command)).resolves.toEqual({
      kind: "stale",
      problem: {
        kind: "stale",
        code: "stale-dependencies",
        title: "Review is out of date",
        detail: "Review dependencies changed after confirmation.",
        correlationId: "corr-stale",
        changes: [{ key: "source:site-report-31", kind: "source_version", reviewed_hash: "v2", current_hash: "v3" }],
      },
    });
  });

  it("keeps a generic review conflict uncertain rather than superseding a possibly applied command", async () => {
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () =>
        HttpResponse.json(
          {
            type: "https://fieldledger.local/problems/review-conflict",
            title: "Request conflict",
            status: 409,
            detail: "review item was already decided",
            correlation_id: "corr-decided",
          },
          { status: 409, headers: { "content-type": "application/problem+json" } },
        ),
      ),
    );

    await expect(sender().send(command)).resolves.toMatchObject({ kind: "reconciliation_required" });
  });

  it.each(["review_ready", "preview_ready"])("treats %s as a reviewable completed UI outcome", async (status) => {
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () => HttpResponse.json({ batch_id: "batch-1", next_batch_id: "batch-2", status })),
      currentReview(),
    );

    await expect(sender().send(command)).resolves.toMatchObject({ kind: "applied", response: { status } });
  });

  it("treats provider PreviewReady as reviewable and stops polling", async () => {
    let pollPosts = 0;
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () => HttpResponse.json({ batch_id: "batch-1", provider_job_id: "job-1", status: "processing" })),
      http.post(`${api}/api/v1/ops/provider-jobs/job-1/poll`, () => {
        pollPosts += 1;
        return HttpResponse.json({ id: "job-1", review_batch_id: "batch-2", status: "PreviewReady" });
      }),
      currentReview(),
    );

    await expect(sender().send(command)).resolves.toMatchObject({ kind: "applied" });
    expect(pollPosts).toBe(1);
  });

  it.each(["failed", "cancelled"])("clears a %s provider outcome as a retryable failure", async (status) => {
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () => HttpResponse.json({ batch_id: "batch-1", status })),
    );

    await expect(sender().send(command)).resolves.toMatchObject({
      kind: "failed",
      problem: { code: status },
    });
  });

  it("maps a polling timeout to a non-loading failure", async () => {
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () => HttpResponse.json({ batch_id: "batch-1", provider_job_id: "job-1", status: "processing" })),
      http.post(`${api}/api/v1/ops/provider-jobs/job-1/poll`, () => HttpResponse.json({ id: "job-1", status: "continue_prompt" })),
    );

    await expect(sender({ maxPolls: 2 }).send(command)).resolves.toMatchObject({
      kind: "failed",
      problem: { code: "timeout" },
    });
  });

  it("routes an unknown response after dispatch to reconciliation", async () => {
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () => HttpResponse.error()),
    );

    await expect(sender().send(command)).resolves.toMatchObject({
      kind: "reconciliation_required",
      problem: { code: "lost-response", correlationId: expect.any(String) },
    });
  });

  it("keeps the local decision and reconciles when the authoritative follow-up read fails", async () => {
    server.use(
      confirmation(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () => HttpResponse.json({ batch_id: "batch-1", status: "completed" })),
      http.get(`${api}/api/v1/reviews/batch-1`, () => HttpResponse.error()),
    );

    await expect(sender().send(command)).resolves.toMatchObject({
      kind: "reconciliation_required",
      problem: { code: "post-refresh-failed", correlationId: expect.any(String) },
    });
  });

  it.each([401, 403, 409, 500])("retains the original request after HTTP %s without minting another token", async (status) => {
    await new SubmissionStore(db).save(command, "original-expired-token");
    let decisionPosts = 0;
    server.use(http.post(`${api}/api/v1/reviews/batch-1/decisions`, async ({ request }) => {
      decisionPosts += 1;
      expect(await request.json()).toMatchObject({ confirmation_token: "original-expired-token" });
      return HttpResponse.json({ type: "https://fieldledger.local/problems/review-conflict" }, { status });
    }));
    // No confirmation handler: a fresh token request would fail the test.
    await expect(sender().reconcile(command)).resolves.toMatchObject({ kind: "reconciliation_required" });
    expect(decisionPosts).toBe(1);
    expect(await new SubmissionStore(db).read(command)).toBe("original-expired-token");
  });

  it("does not request confirmation or dispatch for a missing legacy receipt", async () => {
    await db.submissions.delete(command.id);
    await expect(sender().reconcile(command)).resolves.toMatchObject({
      kind: "reconciliation_required", problem: { code: "submission-unavailable" },
    });
  });

  it("blocks a different phone identity before requesting confirmation", async () => {
    await expect(sender().send({ ...command, clientId: "another-phone" })).resolves.toMatchObject({
      kind: "reconciliation_required", problem: { code: "submission-identity" },
    });
  });

  it("blocks changed decisions even when the caller leaves payloadHash unchanged", async () => {
    await expect(sender().send({ ...command, payload: {
      ...command.payload, decisions: [{ item_id: "item-1", decision: "reject", note: "changed" }],
    } })).resolves.toMatchObject({ kind: "reconciliation_required", problem: { code: "submission-unavailable" } });
  });

  it("never dispatches when confirmation persistence fails", async () => {
    server.use(confirmation());
    db.submissions.hook("updating", () => { throw new Error("storage unavailable"); });
    await expect(sender().send(command)).resolves.toMatchObject({
      kind: "reconciliation_required", problem: { code: "submission-unavailable" },
    });
    expect((await db.submissions.get(command.id))?.token).toBeNull();
  });

  it("does not dispatch a grant for a different reviewed digest", async () => {
    server.use(http.post(`${api}/api/v1/reviews/batch-1/confirmation`, () =>
      HttpResponse.json({ token: "wrong-review", dependency_digest: "different" }),
    ));
    await expect(sender().send(command)).resolves.toMatchObject({ kind: "reconciliation_required", problem: { code: "confirmation-digest" } });
    expect((await db.submissions.get(command.id))?.token).toBeNull();
  });

  it("replays a competing token even when this tab receives a mismatched grant", async () => {
    server.use(
      http.post(`${api}/api/v1/reviews/batch-1/confirmation`, async () => {
        await new SubmissionStore(db).save(command, "other-tab-token");
        return HttpResponse.json({ token: "wrong-review", dependency_digest: "different" });
      }),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, async ({ request }) => {
        expect(await request.json()).toMatchObject({ confirmation_token: "other-tab-token" });
        return HttpResponse.json({ batch_id: "batch-1", status: "completed" });
      }), currentReview(),
    );
    await expect(sender().send(command)).resolves.toMatchObject({ kind: "applied" });
  });

  it("retries a confirmation failure safely before any decision was dispatched", async () => {
    server.use(http.post(`${api}/api/v1/reviews/batch-1/confirmation`, () => HttpResponse.error()));
    await expect(sender().send(command)).resolves.toMatchObject({ kind: "offline" });
    expect((await db.submissions.get(command.id))?.token).toBeNull();
    server.use(confirmation(), currentReview(),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, () => HttpResponse.json({ batch_id: "batch-1", status: "completed" })),
    );
    await expect(sender().send(command)).resolves.toMatchObject({ kind: "applied" });
  });

  it("uses the same persisted winning token when two senders obtain different grants", async () => {
    let grantCount = 0;
    let release!: () => void;
    const bothRequested = new Promise<void>((resolve) => { release = resolve; });
    const postedTokens: string[] = [];
    server.use(
      http.post(`${api}/api/v1/reviews/batch-1/confirmation`, async () => {
        const token = `racing-${++grantCount}`;
        if (grantCount === 2) release();
        await bothRequested;
        return HttpResponse.json({ token, dependency_digest: command.reviewedDigest });
      }),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, async ({ request }) => {
        const body = await request.json() as { confirmation_token: string };
        postedTokens.push(body.confirmation_token);
        return HttpResponse.json({ batch_id: "batch-1", status: "completed" });
      }),
      currentReview(),
    );
    const results = await Promise.all([sender().send(command), sender().reconcile(command)]);
    expect(results.map((result) => result.kind)).toEqual(["applied", "applied"]);
    expect(grantCount).toBe(2);
    expect(postedTokens).toHaveLength(2);
    expect(new Set(postedTokens).size).toBe(1);
  });

  it("uses a competing tab's saved token when the new confirmation request fails", async () => {
    server.use(
      http.post(`${api}/api/v1/reviews/batch-1/confirmation`, async () => {
        await new SubmissionStore(db).save(command, "other-tab-token");
        return HttpResponse.json({ type: "review-conflict" }, { status: 409 });
      }),
      http.post(`${api}/api/v1/reviews/batch-1/decisions`, async ({ request }) => {
        expect(await request.json()).toMatchObject({ confirmation_token: "other-tab-token" });
        return HttpResponse.json({ batch_id: "batch-1", status: "completed" });
      }), currentReview(),
    );
    await expect(sender().send(command)).resolves.toMatchObject({ kind: "applied" });
  });
});
