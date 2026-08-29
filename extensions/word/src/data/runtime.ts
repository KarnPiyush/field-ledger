import { FieldLedgerApiError, FieldLedgerClient } from "@fieldledger/api-client";

export interface WordBackendRuntime {
  client: FieldLedgerClient;
  documentId: string;
  workflowRunId: string;
  reviewBatchId: string;
}

interface BootstrapOptions {
  baseUrl: string;
  fetcher: typeof fetch;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

async function postJson(
  options: BootstrapOptions,
  path: string,
  body: unknown,
  token?: string,
): Promise<unknown> {
  const response = await options.fetcher(new URL(path, options.baseUrl), {
    method: "POST",
    headers: {
      "content-type": "application/json",
      ...(token === undefined ? {} : { authorization: `Bearer ${token}` }),
    },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  let payload: unknown;
  try {
    payload = text.length === 0 ? undefined : JSON.parse(text);
  } catch {
    payload = undefined;
  }
  if (!response.ok) {
    throw new FieldLedgerApiError(response.status, isRecord(payload) ? payload : undefined);
  }
  return payload;
}

export async function bootstrapWordBackend(
  options: BootstrapOptions,
): Promise<WordBackendRuntime> {
  const session = await postJson(options, "/api/v1/auth/demo-session", {
    identity: "reviewer",
  });
  if (!isRecord(session) || typeof session.access_token !== "string") {
    throw new TypeError("Demo session response is invalid");
  }
  const client = new FieldLedgerClient(options.baseUrl, session.access_token, options.fetcher);
  const seed = await client.request("seed_demo", {
    path: {},
    headers: { "Idempotency-Key": "fieldledger-demo-seed:v1" },
    body: {},
  });
  if (
    !isRecord(seed) ||
    typeof seed.document_id !== "string" ||
    typeof seed.workflow_run_id !== "string" ||
    typeof seed.review_batch_id !== "string"
  ) {
    throw new TypeError("Demo seed response is invalid");
  }
  return {
    client,
    documentId: seed.document_id,
    workflowRunId: seed.workflow_run_id,
    reviewBatchId: seed.review_batch_id,
  };
}
