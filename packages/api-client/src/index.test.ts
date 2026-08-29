import { describe, expect, it } from "vitest";

import { FieldLedgerApiError, FieldLedgerClient, operationRegistry } from "./generated.js";

describe("generated FieldLedger API client", () => {
  it("contains the review confirmation and decision operations", () => {
    const paths = Object.values(operationRegistry).map((operation) => operation.path);

    expect(paths).toContain("/api/v1/reviews/{batch_id}/confirmation");
    expect(paths).toContain("/api/v1/reviews/{batch_id}/decisions");
  });

  it("adds the configured bearer token to requests", async () => {
    let capturedInit: RequestInit | undefined;
    const fetcher: typeof fetch = async (_input, init) => {
      capturedInit = init;
      return new Response(
        JSON.stringify({ items: [], total: 0, limit: 50, offset: 0 }),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    };
    const client = new FieldLedgerClient(
      "https://fieldledger.example.test",
      "placeholder-client-token",
      fetcher,
    );

    await client.request("list_projects_api_v1_projects_get", {
      path: {},
      query: { limit: 50, offset: 0 },
      headers: {},
    });

    expect(capturedInit?.headers).toMatchObject({
      authorization: "Bearer placeholder-client-token",
    });
  });

  it("preserves an RFC problem response on typed API errors", async () => {
    const changes = [
      {
        kind: "source_version",
        key: "source:site-report-31",
        reviewed_hash: "sha256:v2",
        current_hash: "sha256:v3",
      },
    ];
    const client = new FieldLedgerClient(
      "https://fieldledger.example.test",
      "session",
      async () =>
        new Response(
          JSON.stringify({
            type: "https://fieldledger.local/problems/review-stale",
            title: "Review is stale",
            status: 409,
            detail: "Review dependencies changed after confirmation.",
            instance: "/api/v1/reviews/batch-1/decisions",
            correlation_id: "corr-stale",
            changes,
          }),
          {
            status: 409,
            headers: {
              "content-type": "application/problem+json",
              "x-correlation-id": "corr-stale",
            },
          },
        ),
    );

    const request = client.request(
      "submit_decisions_api_v1_reviews__batch_id__decisions_post",
      {
        path: { batch_id: "batch-1" },
        headers: { "Idempotency-Key": "phone:1" },
        body: { confirmation_token: "token", decisions: [] },
      },
    );

    await expect(request).rejects.toEqual(
      expect.objectContaining({
        name: "FieldLedgerApiError",
        status: 409,
        problem: expect.objectContaining({
          title: "Review is stale",
          correlation_id: "corr-stale",
          changes,
        }),
      }),
    );
    await expect(request).rejects.toBeInstanceOf(FieldLedgerApiError);
  });
});
