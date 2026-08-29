import { FieldLedgerClient } from "@fieldledger/api-client";
import { HttpResponse, http } from "msw";
import { setupServer } from "msw/node";
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { MobileQueueSender } from "./queueSender";
import type { MobileDecisionCommand } from "../data/types";

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

function sender(options: { online?: boolean; maxPolls?: number } = {}) {
  return new MobileQueueSender(new FieldLedgerClient(api, "test-session"), {
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
afterEach(() => server.resetHandlers());
afterAll(() => server.close());

describe("mobile queue sender", () => {
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

  it("refreshes authority when another client already decided the batch", async () => {
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

    await expect(sender().send(command)).resolves.toEqual({
      kind: "stale",
      problem: {
        kind: "stale",
        code: "stale-dependencies",
        title: "Review is out of date",
        detail: "review item was already decided",
        correlationId: "corr-decided",
        changes: [],
      },
    });
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
});
