import { type FieldLedgerClient } from "@fieldledger/api-client";
import { encodeQueue, type QueueCommand } from "@fieldledger/offline-queue";
import { describe, expect, it, vi } from "vitest";

import type {
  DocumentFingerprint,
  SerializedQueue,
  WordBridge,
  WordInstruction,
} from "../office/bridge";
import { DocumentQueueStore } from "./queueStore";
import {
  WordQueueSender,
  FieldLedgerProposalGateway,
  replayQueue,
  type ProposalGateway,
  type ProposalStatus,
} from "./replay";

const reviewed: DocumentFingerprint = {
  documentId: "document-1",
  contentDigest: "body-reviewed",
  selectionDigest: "selection-reviewed",
  selectionPreview: "reviewed selection",
};

const instruction = (kind: "document" | "selection" = "document"): WordInstruction => ({
  text: `Update the ${kind}`,
  base: reviewed,
  target: { kind, preview: kind === "selection" ? "reviewed selection" : "Current document" },
});

describe("FieldLedger Word gateway", () => {
  async function queuedInstruction() {
    const store = new DocumentQueueStore(new ReplayBridge());
    return add(store, instruction(), 1);
  }

  it("reconciles by durable idempotency lookup without starting another edit", async () => {
    const request = vi.fn().mockResolvedValue({ id: "job-1", status: "processing" });
    const gateway = new FieldLedgerProposalGateway(
      { request } as unknown as FieldLedgerClient,
      {
        documentId: "document-1",
        workflowRunId: "run-1",
        reviewBaseUrl: "https://mobile.fieldledger.test",
      },
    );
    const command = await queuedInstruction();

    await gateway.reconcile(command);

    expect(request).toHaveBeenCalledOnce();
    expect(request).toHaveBeenCalledWith("lookup_edit_by_idempotency_key", {
      path: { document_id: "document-1" },
      headers: { "Idempotency-Key": command.idempotencyKey },
    });
  });

  it("advances provider jobs through the mutating poll endpoint", async () => {
    const request = vi.fn().mockResolvedValue({ id: "job-1", status: "preview_ready" });
    const gateway = new FieldLedgerProposalGateway(
      { request } as unknown as FieldLedgerClient,
      {
        documentId: "document-1",
        workflowRunId: "run-1",
        reviewBaseUrl: "https://mobile.fieldledger.test",
      },
    );

    await gateway.poll("job-1");

    expect(request).toHaveBeenCalledWith(
      "poll_provider_job_api_v1_ops_provider_jobs__job_id__poll_post",
      {
        path: { job_id: "job-1" },
        body: {},
      },
    );
  });

  it("hands review-ready jobs to the mobile singular review route", async () => {
    const request = vi.fn().mockResolvedValue({
      id: "job-1",
      review_batch_id: "batch/one",
      status: "PreviewReady",
    });
    const gateway = new FieldLedgerProposalGateway(
      { request } as unknown as FieldLedgerClient,
      {
        documentId: "document-1",
        workflowRunId: "run-1",
        reviewBaseUrl: "https://mobile.fieldledger.test/base",
      },
    );

    await expect(gateway.start(await queuedInstruction())).resolves.toMatchObject({
      reviewUrl: "https://mobile.fieldledger.test/review/batch%2Fone/1",
    });
  });
});

class ReplayBridge implements WordBridge {
  public serialized: SerializedQueue = encodeQueue([]);
  public fingerprints: Array<DocumentFingerprint | Error> = [reviewed];

  public async ready(): Promise<void> {}
  public async getFingerprint(): Promise<DocumentFingerprint> {
    const next = this.fingerprints.length > 1 ? this.fingerprints.shift() : this.fingerprints[0];
    if (next instanceof Error) throw next;
    if (!next) throw new Error("No fingerprint configured");
    return next;
  }
  public async loadQueue(): Promise<SerializedQueue> {
    return this.serialized;
  }
  public async saveQueue(queue: SerializedQueue): Promise<void> {
    this.serialized = queue;
  }
  public async openReview(): Promise<void> {}
}

class FakeGateway implements ProposalGateway {
  public starts: QueueCommand<WordInstruction>[] = [];
  public reconciles: QueueCommand<WordInstruction>[] = [];
  public polls = 0;

  public constructor(
    public startResult: ProposalStatus | Error = {
      status: "PreviewReady",
      jobId: "job-1",
      reviewUrl: "https://fieldledger.test/reviews/batch-1",
    },
    public pollResults: Array<ProposalStatus | Error> = [],
  ) {}

  public async start(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    this.starts.push(command);
    if (this.startResult instanceof Error) throw this.startResult;
    return this.startResult;
  }

  public async reconcile(command: QueueCommand<WordInstruction>): Promise<ProposalStatus> {
    this.reconciles.push(command);
    if (this.startResult instanceof Error) throw this.startResult;
    return this.startResult;
  }

  public async poll(): Promise<ProposalStatus> {
    this.polls += 1;
    const result = this.pollResults.shift() ?? this.startResult;
    if (result instanceof Error) throw result;
    return result;
  }
}

async function add(store: DocumentQueueStore, value: WordInstruction, index: number) {
  return store.enqueue(value, {
    id: `command-${index}`,
    deduplicationKey: `draft-${index}`,
    createdAt: `2026-08-23T12:00:0${index}.000Z`,
  });
}

describe("Word queue replay", () => {
  it("replays pending instructions strictly in sequence and records review handoff", async () => {
    const bridge = new ReplayBridge();
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction(), 1);
    await add(store, { ...instruction(), text: "Update the register" }, 2);
    const gateway = new FakeGateway();
    const reviews: string[] = [];
    const sender = new WordQueueSender(bridge, gateway, {
      isOnline: () => true,
      onReviewReady: (url) => reviews.push(url),
    });

    await expect(replayQueue(store, sender)).resolves.toMatchObject({
      status: "drained",
      applied: 2,
    });

    expect(gateway.starts.map((command) => command.sequence)).toEqual([1, 2]);
    expect((await store.list()).map((command) => command.state)).toEqual(["applied", "applied"]);
    expect(reviews).toEqual([
      "https://fieldledger.test/reviews/batch-1",
      "https://fieldledger.test/reviews/batch-1",
    ]);
  });

  it("marks a changed document stale and stops before later instructions", async () => {
    const bridge = new ReplayBridge();
    bridge.fingerprints = [{ ...reviewed, contentDigest: "body-current" }];
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction("document"), 1);
    await add(store, { ...instruction("document"), text: "Later instruction" }, 2);
    const gateway = new FakeGateway();

    await expect(
      replayQueue(store, new WordQueueSender(bridge, gateway, { isOnline: () => true })),
    ).resolves.toMatchObject({ status: "stale", commandId: "command-1" });

    const commands = await store.list();
    expect(commands.map((command) => command.state)).toEqual(["stale", "pending"]);
    expect(commands[0]?.lastProblem).toMatchObject({
      kind: "stale",
      detail: "Nothing was applied",
    });
    expect(gateway.starts).toHaveLength(0);
  });

  it("stops before send when a selection moved even if the body digest is unchanged", async () => {
    const bridge = new ReplayBridge();
    bridge.fingerprints = [{
      ...reviewed,
      selectionDigest: "selection-current",
      selectionPreview: "current selection",
    }];
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction("selection"), 1);
    const gateway = new FakeGateway();

    await replayQueue(store, new WordQueueSender(bridge, gateway, { isOnline: () => true }));

    expect((await store.list())[0]).toMatchObject({ state: "stale" });
    expect(gateway.starts).toHaveLength(0);
  });

  it("stops before send when the document identity differs", async () => {
    const bridge = new ReplayBridge();
    bridge.fingerprints = [{ ...reviewed, documentId: "document-2" }];
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction(), 1);
    const gateway = new FakeGateway();

    await replayQueue(store, new WordQueueSender(bridge, gateway, { isOnline: () => true }));

    expect((await store.list())[0]).toMatchObject({ state: "stale" });
    expect(gateway.starts).toHaveLength(0);
  });

  it("leaves definitely-not-sent work pending while offline", async () => {
    const bridge = new ReplayBridge();
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction(), 1);
    const gateway = new FakeGateway();

    await expect(
      replayQueue(store, new WordQueueSender(bridge, gateway, { isOnline: () => false })),
    ).resolves.toMatchObject({ status: "offline" });

    expect((await store.list())[0]).toMatchObject({ state: "pending", attempts: 1 });
    expect(gateway.starts).toHaveLength(0);
  });

  it("reconciles a lost response with the same idempotency key without a second start", async () => {
    const bridge = new ReplayBridge();
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction(), 1);
    const gateway = new FakeGateway(new Error("response lost"));
    const log = vi.fn();
    const sender = new WordQueueSender(bridge, gateway, { isOnline: () => true, log });

    await expect(replayQueue(store, sender)).resolves.toMatchObject({
      status: "reconciliation_required",
    });
    const ambiguous = (await store.list())[0];
    expect(ambiguous).toMatchObject({ state: "reconciliation_required" });

    gateway.startResult = {
      status: "PreviewReady",
      jobId: "job-1",
      reviewUrl: "https://fieldledger.test/reviews/batch-1",
    };
    await store.prepareReconciliation("command-1");
    await replayQueue(store, sender);

    expect(gateway.starts).toHaveLength(1);
    expect(gateway.reconciles).toHaveLength(1);
    expect(gateway.reconciles[0]?.idempotencyKey).toBe(gateway.starts[0]?.idempotencyKey);
    expect((await store.list())[0]).toMatchObject({ state: "applied" });
    expect(log).toHaveBeenCalledWith("Proposal response lost after dispatch", {
      commandId: "command-1",
      correlationId: expect.stringMatching(/^word-/),
      error: "response lost",
    });
  });

  it.each(["review_ready", "preview_ready", "PreviewReady", "awaiting_approval"])(
    "treats %s as a completed reviewable outcome",
    async (status) => {
      const bridge = new ReplayBridge();
      const store = new DocumentQueueStore(bridge);
      await add(store, instruction(), 1);
      const onReviewReady = vi.fn();
      const gateway = new FakeGateway({
        status,
        jobId: "job-1",
        reviewUrl: "https://fieldledger.test/reviews/batch-1",
      });

      await replayQueue(
        store,
        new WordQueueSender(bridge, gateway, { isOnline: () => true, onReviewReady }),
      );

      expect((await store.list())[0]).toMatchObject({ state: "applied" });
      expect(onReviewReady).toHaveBeenCalledOnce();
    },
  );

  it("keeps continue_prompt processing until PreviewReady", async () => {
    const bridge = new ReplayBridge();
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction(), 1);
    const phases: string[] = [];
    const gateway = new FakeGateway(
      { status: "continue_prompt", jobId: "job-1" },
      [{ status: "PreviewReady", jobId: "job-1", reviewUrl: "/review.html" }],
    );

    await replayQueue(
      store,
      new WordQueueSender(bridge, gateway, {
        isOnline: () => true,
        wait: async () => undefined,
        onOutcome: (outcome) => phases.push(outcome),
      }),
    );

    expect(phases).toEqual(["continue_prompt", "PreviewReady"]);
    expect(gateway.polls).toBe(1);
    expect((await store.list())[0]).toMatchObject({ state: "applied" });
  });

  it.each(["failed", "cancelled"])("clears sending and retains %s instructions", async (status) => {
    const bridge = new ReplayBridge();
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction(), 1);

    await replayQueue(
      store,
      new WordQueueSender(bridge, new FakeGateway({ status, jobId: "job-1" }), {
        isOnline: () => true,
      }),
    );

    expect((await store.list())[0]).toMatchObject({
      state: "failed",
      payload: { text: "Update the document" },
    });
  });

  it.each(["made_up", "timed_out", "queued"])(
    "routes unknown terminal status %s to reconciliation",
    async (status) => {
      const bridge = new ReplayBridge();
      const store = new DocumentQueueStore(bridge);
      await add(store, instruction(), 1);

      await replayQueue(
        store,
        new WordQueueSender(bridge, new FakeGateway({ status, jobId: "job-1" }), {
          isOnline: () => true,
        }),
      );

      expect((await store.list())[0]).toMatchObject({
        state: "reconciliation_required",
        lastProblem: { code: "unknown-provider-status" },
      });
    },
  );

  it("moves to reconciliation when the post-send document read fails", async () => {
    const bridge = new ReplayBridge();
    bridge.fingerprints = [reviewed, new Error("Word read failed")];
    const store = new DocumentQueueStore(bridge);
    await add(store, instruction(), 1);

    const log = vi.fn();
    await replayQueue(
      store,
      new WordQueueSender(bridge, new FakeGateway(), { isOnline: () => true, log }),
    );

    expect((await store.list())[0]).toMatchObject({
      state: "reconciliation_required",
      lastProblem: { code: "post-send-read-failed" },
    });
    expect(log).toHaveBeenCalledWith("Post-send Word read failed", {
      commandId: "command-1",
      correlationId: expect.stringMatching(/^word-/),
      error: "Word read failed",
    });
  });
});
