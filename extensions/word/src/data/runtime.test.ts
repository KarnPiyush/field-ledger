import { describe, expect, it } from "vitest";

import { bootstrapWordBackend } from "./runtime";

describe("Word backend bootstrap", () => {
  it("uses the proxied reviewer session and authoritative seed identifiers", async () => {
    const seen: Array<{ path: string; headers: Headers; body: unknown }> = [];
    const fetcher: typeof fetch = async (input, init) => {
      const path = new URL(String(input)).pathname;
      seen.push({
        path,
        headers: new Headers(init?.headers),
        body: init?.body ? JSON.parse(String(init.body)) : undefined,
      });
      if (path === "/api/v1/auth/demo-session") {
        return new Response(JSON.stringify({
          access_token: "signed-word-session",
          actor_id: "00000000-0000-4000-8000-000000000301",
          global_role: "approver",
          source: "demo",
          token_type: "bearer",
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      if (path === "/api/v1/demo/seed") {
        return new Response(JSON.stringify({
          document_id: "00000000-0000-4000-8000-000000000501",
          project_id: "00000000-0000-4000-8000-000000000101",
          workflow_run_id: "00000000-0000-4000-8000-000000000401",
          review_batch_id: "00000000-0000-4000-8000-000000000201",
          status: "ready",
          replayed: false,
        }), { status: 200, headers: { "content-type": "application/json" } });
      }
      throw new Error(`Unhandled ${path}`);
    };

    const runtime = await bootstrapWordBackend({
      baseUrl: "https://word.fieldledger.test",
      fetcher,
    });

    expect(runtime).toMatchObject({
      documentId: "00000000-0000-4000-8000-000000000501",
      workflowRunId: "00000000-0000-4000-8000-000000000401",
    });
    expect(seen).toEqual([
      {
        path: "/api/v1/auth/demo-session",
        headers: expect.any(Headers),
        body: { identity: "reviewer" },
      },
      {
        path: "/api/v1/demo/seed",
        headers: expect.any(Headers),
        body: {},
      },
    ]);
    expect(seen[0]?.headers.has("X-FieldLedger-Demo-Bootstrap")).toBe(false);
    expect(seen[1]?.headers.get("authorization")).toBe("Bearer signed-word-session");
  });
});
