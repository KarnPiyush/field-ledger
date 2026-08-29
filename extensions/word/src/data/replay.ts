import {
  FieldLedgerClient,
  type components,
} from "@fieldledger/api-client";
import {
  drainInOrder,
  type DrainSummary,
  type QueueCommand,
  type QueueProblem,
  type Sender,
  type SendResult,
} from "@fieldledger/offline-queue";

import type {
  DocumentFingerprint,
  WordBridge,
  WordInstruction,
} from "../office/bridge";
import type { AsyncOutcome } from "../state/asyncPhase";
import type { DocumentQueueStore } from "./queueStore";

export interface ProposalStatus {
  status: string;
  jobId: string;
  batchId?: string;
  reviewUrl?: string;
}

export interface ProposalGateway {
  start(command: QueueCommand<WordInstruction>): Promise<ProposalStatus>;
  reconcile(command: QueueCommand<WordInstruction>): Promise<ProposalStatus>;
  poll(jobId: string): Promise<ProposalStatus>;
}

interface SenderOptions {
  isOnline: () => boolean;
  maxPolls: number;
  wait: (milliseconds: number) => Promise<void>;
  onReviewReady: (url: string) => void;
  onOutcome: (outcome: AsyncOutcome) => void;
  log: (message: string, context: Record<string, string>) => void;
}

const defaults: SenderOptions = {
  isOnline: () => navigator.onLine,
  maxPolls: 8,
  wait: (milliseconds) => new Promise((resolve) => window.setTimeout(resolve, milliseconds)),
  onReviewReady: () => undefined,
  onOutcome: () => undefined,
  log: (message, context) => console.error(message, context),
};

function normalizedStatus(status: string): string {
  return status.toLowerCase().replaceAll("_", "").replaceAll("-", "");
}

function failed(code: string, title: string, detail: string): SendResult {
  return { kind: "failed", problem: { kind: "failed", code, title, detail } };
}

function reconcile(
  code: string,
  title: string,
  detail: string,
  correlationId = `word-${crypto.randomUUID()}`,
): SendResult {
  return {
    kind: "reconciliation_required",
    problem: { kind: "reconciliation_required", code, title, detail, correlationId },
  };
}

function changed(
  key: string,
  reviewedHash: string | null,
  currentHash: string | null,
): components["schemas"]["DependencyChange"] {
  return {
    kind: "target_document_version",
    key,
    reviewed_hash: reviewedHash,
    current_hash: currentHash,
  };
}

function staleProblem(
  instruction: WordInstruction,
  current: DocumentFingerprint,
): Extract<QueueProblem, { kind: "stale" }> | null {
  const changes: components["schemas"]["DependencyChange"][] = [];
  if (instruction.base.documentId !== current.documentId) {
    changes.push(
      changed("document-id", instruction.base.documentId, current.documentId),
    );
  } else if (
    instruction.target.kind === "document" &&
    instruction.base.contentDigest !== current.contentDigest
  ) {
    changes.push(
      changed(
        `${instruction.base.documentId}:body`,
        instruction.base.contentDigest,
        current.contentDigest,
      ),
    );
  } else if (
    instruction.target.kind === "selection" &&
    instruction.base.selectionDigest !== current.selectionDigest
  ) {
    changes.push(
      changed(
        `${instruction.base.documentId}:selection`,
        instruction.base.selectionDigest,
        current.selectionDigest,
      ),
    );
  }
  if (changes.length === 0) return null;
  return {
    kind: "stale",
    code: "word-fingerprint-moved",
    title: "Needs refresh",
    detail: "Nothing was applied",
    changes,
  };
}

function applied(status: ProposalStatus): SendResult {
  const normalized = normalizedStatus(status.status);
  const apiStatus: components["schemas"]["ApplyResult"]["status"] =
    normalized === "reviewready" ||
    normalized === "previewready" ||
    normalized === "awaitingapproval"
      ? "preview_ready"
      : "completed";
  return {
    kind: "applied",
    response: {
      batch_id: status.batchId ?? status.jobId,
      provider_job_id: status.jobId,
      status: apiStatus,
    },
  };
}

function isReviewable(status: string): boolean {
  const normalized = normalizedStatus(status);
  return (
    normalized === "reviewready" ||
    normalized === "previewready" ||
    normalized === "awaitingapproval"
  );
}

function phaseOutcome(status: string): AsyncOutcome {
  if (status === "PreviewReady") return "PreviewReady";
  const normalized = status.toLowerCase();
  if (normalized === "continue_prompt") return "continue_prompt";
  if (normalized === "awaiting_approval") return "awaiting_approval";
  if (normalized === "review_ready") return "review_ready";
  if (normalized === "preview_ready") return "preview_ready";
  if (normalized === "cancelled") return "cancelled";
  if (normalized === "failed") return "failed";
  if (normalized === "completed") return "completed";
  return "processing";
}

export class WordQueueSender implements Sender<WordInstruction> {
  private readonly options: SenderOptions;

  public constructor(
    private readonly bridge: WordBridge,
    private readonly gateway: ProposalGateway,
    options: Partial<SenderOptions> = {},
  ) {
    this.options = { ...defaults, ...options };
  }

  public send(command: QueueCommand<WordInstruction>): Promise<SendResult> {
    return this.submit(command, false);
  }

  public reconcile(command: QueueCommand<WordInstruction>): Promise<SendResult> {
    return this.submit(command, true);
  }

  private async submit(
    command: QueueCommand<WordInstruction>,
    reconciliation: boolean,
  ): Promise<SendResult> {
    if (!this.options.isOnline()) {
      this.options.onOutcome("offline");
      return {
        kind: "offline",
        problem: {
          kind: "offline",
          code: "offline",
          title: "Waiting to sync",
          detail: "The instruction remains saved in this document.",
        },
      };
    }

    let beforeSend: DocumentFingerprint;
    try {
      beforeSend = await this.bridge.getFingerprint();
    } catch (error) {
      this.options.log("Word fingerprint preflight failed", {
        commandId: command.id,
        error: error instanceof Error ? error.message : String(error),
      });
      this.options.onOutcome("fetch_error");
      return failed(
        "fingerprint-read-failed",
        "Could not read this document",
        "Nothing was applied. Retry after Word can read the document.",
      );
    }
    const stale = staleProblem(command.payload, beforeSend);
    if (stale) {
      this.options.onOutcome("stale");
      return { kind: "stale", problem: stale };
    }

    let status: ProposalStatus;
    try {
      status = reconciliation
        ? await this.gateway.reconcile(command)
        : await this.gateway.start(command);
    } catch (error) {
      const correlationId = `word-${crypto.randomUUID()}`;
      this.options.log("Proposal response lost after dispatch", {
        commandId: command.id,
        correlationId,
        error: error instanceof Error ? error.message : String(error),
      });
      this.options.onOutcome("lost_response");
      return reconcile(
        "lost-response",
        "Needs reconciliation",
        "The response was lost after dispatch. The instruction remains available for reconciliation.",
        correlationId,
      );
    }

    for (let poll = 0; normalizedStatus(status.status) === "processing" || normalizedStatus(status.status) === "continueprompt"; poll += 1) {
      this.options.onOutcome(phaseOutcome(status.status));
      if (poll >= this.options.maxPolls) {
        this.options.onOutcome("timeout");
        return failed(
          "timeout",
          "Proposal timed out",
          "Nothing was applied. Retry the status check.",
        );
      }
      await this.options.wait(250 * (poll + 1));
      try {
        status = await this.gateway.poll(status.jobId);
      } catch (error) {
        const correlationId = `word-${crypto.randomUUID()}`;
        this.options.log("Proposal status read failed", {
          commandId: command.id,
          correlationId,
          error: error instanceof Error ? error.message : String(error),
        });
        this.options.onOutcome("stream_error");
        return reconcile(
          "stream-error",
          "Needs reconciliation",
          "Proposal progress could not be confirmed.",
          correlationId,
        );
      }
    }

    this.options.onOutcome(phaseOutcome(status.status));
    const normalized = normalizedStatus(status.status);
    if (normalized === "failed" || normalized === "cancelled") {
      return failed(
        normalized,
        normalized === "failed" ? "Proposal failed" : "Proposal cancelled",
        "Nothing was applied. The instruction remains available to retry.",
      );
    }
    if (normalized === "reconciliationrequired") {
      return reconcile(
        "provider-reconciliation",
        "Needs reconciliation",
        "The proposal outcome is not authoritative yet.",
      );
    }
    if (normalized !== "completed" && !isReviewable(status.status)) {
      return reconcile(
        "unknown-provider-status",
        "Needs reconciliation",
        `Provider returned an unsupported status: ${status.status}`,
      );
    }

    try {
      const afterSend = await this.bridge.getFingerprint();
      if (staleProblem(command.payload, afterSend)) {
        this.options.onOutcome("post_refresh_error");
        return reconcile(
          "post-send-document-moved",
          "Needs reconciliation",
          "The document moved while the proposal was being submitted.",
        );
      }
    } catch (error) {
      const correlationId = `word-${crypto.randomUUID()}`;
      this.options.log("Post-send Word read failed", {
        commandId: command.id,
        correlationId,
        error: error instanceof Error ? error.message : String(error),
      });
      this.options.onOutcome("post_refresh_error");
      return reconcile(
        "post-send-read-failed",
        "Needs reconciliation",
        "The proposal was submitted, but Word could not confirm the current document.",
        correlationId,
      );
    }

    if (isReviewable(status.status) && status.reviewUrl) {
      this.options.onReviewReady(status.reviewUrl);
    }
    return applied(status);
  }
}

export function replayQueue(
  store: DocumentQueueStore,
  sender: Sender<WordInstruction>,
): Promise<DrainSummary> {
  return drainInOrder(store, sender);
}

export interface FieldLedgerGatewayOptions {
  documentId: string;
  workflowRunId: string;
  reviewBaseUrl: string;
}

export class FieldLedgerProposalGateway implements ProposalGateway {
  public constructor(
    private readonly client: FieldLedgerClient,
    private readonly options: FieldLedgerGatewayOptions,
  ) {}

  public start(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    return this.submit(command);
  }

  public reconcile(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    return this.lookup(command);
  }

  public async poll(jobId: string): Promise<ProposalStatus> {
    const response = await this.client.request(
      "poll_provider_job_api_v1_ops_provider_jobs__job_id__poll_post",
      {
        path: { job_id: jobId },
        body: {},
      },
    );
    return this.fromResponse(response);
  }

  private async submit(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    const response = await this.client.request(
      "start_edit_api_v1_documents__document_id__edits_post",
      {
        path: { document_id: this.options.documentId },
        headers: { "Idempotency-Key": command.idempotencyKey },
        body: {
          prompt: command.payload.text,
          workflow_run_id: this.options.workflowRunId,
        },
      },
    );
    return this.fromResponse(response);
  }

  private async lookup(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    const response = await this.client.request("lookup_edit_by_idempotency_key", {
      path: { document_id: this.options.documentId },
      headers: { "Idempotency-Key": command.idempotencyKey },
    });
    return this.fromResponse(response);
  }

  private fromResponse(response: components["schemas"]["ProviderJobResponse"]): ProposalStatus {
    return {
      status: response.status,
      jobId: response.id,
      ...(response.review_batch_id === undefined || response.review_batch_id === null
        ? {}
        : {
            batchId: response.review_batch_id,
            reviewUrl: new URL(
              `/review/${encodeURIComponent(response.review_batch_id)}/1`,
              this.options.reviewBaseUrl,
            ).toString(),
          }),
    };
  }
}
