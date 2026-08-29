import {
  FieldLedgerApiError,
  FieldLedgerClient,
  type components,
} from "@fieldledger/api-client";
import type {
  QueueProblem,
  Sender,
  SendResult,
} from "@fieldledger/offline-queue";

import type { MobileDecisionCommand, MobileDecisionPayload } from "../data/types";

interface QueueSenderOptions {
  isOnline: () => boolean;
  maxPolls: number;
  wait: (milliseconds: number) => Promise<void>;
  log: (message: string, context: Record<string, string>) => void;
}

const defaults: QueueSenderOptions = {
  isOnline: () => navigator.onLine,
  maxPolls: 8,
  wait: (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds)),
  log: (message, context) => console.error(message, context),
};

function correlationId(): string {
  return `mobile-${crypto.randomUUID()}`;
}

function failed(code: string, title: string, detail: string): SendResult {
  return { kind: "failed", problem: { kind: "failed", code, title, detail } };
}

function reconcile(code: string, title: string, detail: string, id: string): SendResult {
  return {
    kind: "reconciliation_required",
    problem: { kind: "reconciliation_required", code, title, detail, correlationId: id },
  };
}

function applied(response: components["schemas"]["ApplyResult"]): SendResult {
  return { kind: "applied", response };
}

function staleFromError(
  error: unknown,
): Extract<SendResult, { kind: "stale" }> | null {
  if (!(error instanceof FieldLedgerApiError) || error.status !== 409) return null;
  const rawChanges = error.problem?.changes;
  const changes: unknown[] | null = Array.isArray(rawChanges)
    ? rawChanges
    : String(error.problem?.type).endsWith("/review-conflict")
      ? []
      : null;
  if (changes === null) return null;
  const valid = changes.every(
    (change) =>
      change !== null &&
      typeof change === "object" &&
      typeof (change as { key?: unknown }).key === "string" &&
      typeof (change as { kind?: unknown }).kind === "string" &&
      ((change as { reviewed_hash?: unknown }).reviewed_hash === null ||
        typeof (change as { reviewed_hash?: unknown }).reviewed_hash === "string") &&
      ((change as { current_hash?: unknown }).current_hash === null ||
        typeof (change as { current_hash?: unknown }).current_hash === "string"),
  );
  if (!valid) return null;
  return {
    kind: "stale",
    problem: {
      kind: "stale",
      code: "stale-dependencies",
      title: "Review is out of date",
      detail: error.problem?.detail ?? "Nothing was applied",
      changes: changes as components["schemas"]["DependencyChange"][],
      ...(typeof error.problem?.correlation_id === "string"
        ? { correlationId: error.problem.correlation_id }
        : {}),
    },
  };
}

export class MobileQueueSender implements Sender<MobileDecisionPayload> {
  private readonly options: QueueSenderOptions;

  public constructor(
    private readonly client: FieldLedgerClient,
    options: Partial<QueueSenderOptions> = {},
  ) {
    this.options = { ...defaults, ...options };
  }

  public send(command: MobileDecisionCommand): Promise<SendResult> {
    return this.submit(command);
  }

  public reconcile(command: MobileDecisionCommand): Promise<SendResult> {
    return this.submit(command);
  }

  private async submit(command: MobileDecisionCommand): Promise<SendResult> {
    if (!this.options.isOnline()) {
      return {
        kind: "offline",
        problem: {
          kind: "offline",
          code: "offline",
          title: "Waiting to sync",
          detail: "The decision remains saved on this phone.",
        },
      };
    }

    let token: string;
    try {
      const confirmation = await this.client.request(
        "request_confirmation_api_v1_reviews__batch_id__confirmation_post",
        { path: { batch_id: command.payload.batchId }, body: {} },
      );
      token = confirmation.token;
    } catch (error) {
      return this.beforeDispatchFailure(error);
    }

    let response: components["schemas"]["ApplyResult"];
    try {
      response = await this.client.request("submit_decisions_api_v1_reviews__batch_id__decisions_post", {
        path: { batch_id: command.payload.batchId },
        headers: { "Idempotency-Key": command.idempotencyKey },
        body: { confirmation_token: token, decisions: command.payload.decisions },
      });
    } catch (error) {
      const stale = staleFromError(error);
      if (stale !== null) return stale;
      const id = correlationId();
      this.options.log("Decision response lost after dispatch", { correlationId: id });
      return reconcile(
        "lost-response",
        "Needs reconciliation",
        "The response was lost after dispatch. FieldLedger will verify before reporting an outcome.",
        id,
      );
    }

    if (response.status === "stale") {
      const problem: Extract<QueueProblem, { kind: "stale" }> = {
        kind: "stale",
        code: "stale-dependencies",
        title: "Review is out of date",
        detail: "Nothing was applied",
        changes: response.stale_changes ?? [],
      };
      return { kind: "stale", problem };
    }
    if (response.status === "failed" || response.status === "cancelled") {
      return failed(response.status, response.status === "failed" ? "Decision failed" : "Decision cancelled", "Nothing was applied. The saved decision remains available to retry.");
    }
    if (response.status === "reconciliation_required") {
      const id = correlationId();
      return reconcile("provider-reconciliation", "Needs reconciliation", "The provider outcome is not authoritative yet.", id);
    }
    if (response.status === "processing") {
      const providerResult = await this.pollProvider(response);
      if (providerResult !== null) return providerResult;
    }

    return this.refreshAuthority(command, response);
  }

  private beforeDispatchFailure(error: unknown): SendResult {
    if (!this.options.isOnline() || error instanceof TypeError) {
      return {
        kind: "offline",
        problem: {
          kind: "offline",
          code: "offline",
          title: "Waiting to sync",
          detail: "The decision remains saved on this phone.",
        },
      };
    }
    return failed("fetch-error", "Could not contact FieldLedger", "Retry when the connection is available.");
  }

  private async pollProvider(
    response: components["schemas"]["ApplyResult"],
  ): Promise<SendResult | null> {
    if (!response.provider_job_id) {
      const id = correlationId();
      return reconcile("unknown-provider-job", "Needs reconciliation", "Processing started without a provider job reference.", id);
    }
    for (let attempt = 0; attempt < this.options.maxPolls; attempt += 1) {
      await this.options.wait(250 * (attempt + 1));
      let job: components["schemas"]["ProviderJobResponse"];
      try {
        job = await this.client.request(
          "poll_provider_job_api_v1_ops_provider_jobs__job_id__poll_post",
          {
            path: { job_id: response.provider_job_id },
            body: {},
          },
        );
      } catch {
        const id = correlationId();
        this.options.log("Provider stream or poll failed", { correlationId: id });
        return reconcile("stream-error", "Needs reconciliation", "Provider progress could not be confirmed.", id);
      }
      const normalized = job.status.toLowerCase().replaceAll("_", "");
      if (normalized === "previewready" || normalized === "awaitingapproval" || normalized === "reviewready") {
        return null;
      }
      if (normalized === "completed") return null;
      if (normalized === "failed" || normalized === "cancelled") {
        return failed(normalized, normalized === "failed" ? "Provider failed" : "Provider cancelled", "Nothing was applied. The command is ready for operator recovery.");
      }
    }
    return failed("timeout", "Provider timed out", "Processing took too long. Retry the status check.");
  }

  private async refreshAuthority(
    command: MobileDecisionCommand,
    response: components["schemas"]["ApplyResult"],
  ): Promise<SendResult> {
    try {
      await this.client.request("get_review_api_v1_reviews__batch_id__get", {
        path: { batch_id: command.payload.batchId },
      });
      return applied(response);
    } catch {
      const id = correlationId();
      this.options.log("Post-mutation review refresh failed", { correlationId: id });
      return reconcile(
        "post-refresh-failed",
        "Saved decision needs verification",
        "The server accepted the command, but the authoritative refresh failed.",
        id,
      );
    }
  }
}
