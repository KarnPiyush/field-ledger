import { describe, expect, it } from "vitest";

import {
  asyncPhaseReducer,
  canBeginPhase,
  createAsyncPhases,
  phaseFor,
  type AsyncOutcome,
} from "./asyncPhase";

describe("scoped async phases", () => {
  it("clears stale messages when the same command enters a new phase", () => {
    const failed = asyncPhaseReducer(createAsyncPhases(), {
      type: "finish",
      scope: "queue:4",
      outcome: "fetch_error",
      message: "Network request failed",
    });

    const pending = asyncPhaseReducer(failed, { type: "begin", scope: "queue:4" });

    expect(phaseFor(pending, "queue:4")).toEqual({ status: "pending", busy: true });
    expect(canBeginPhase(pending, "queue:4")).toBe(false);
    expect(phaseFor(pending, "queue:9")).toEqual({ status: "idle", busy: false });
  });

  it.each([
    ["awaiting_approval", "review"],
    ["review_ready", "review"],
    ["preview_ready", "review"],
    ["PreviewReady", "review"],
    ["completed", "success"],
    ["applied", "success"],
    ["stale", "attention"],
    ["failed", "error"],
    ["cancelled", "error"],
    ["timeout", "error"],
    ["offline", "error"],
    ["fetch_error", "error"],
    ["stream_error", "error"],
    ["post_refresh_error", "error"],
    ["reconciliation_required", "reconcile"],
    ["lost_response", "reconcile"],
    ["unknown", "reconcile"],
  ] satisfies Array<[AsyncOutcome, string]>)
    ("maps %s to non-loading %s state", (outcome, status) => {
      const pending = asyncPhaseReducer(createAsyncPhases(), {
        type: "begin",
        scope: "decision:item-1",
      });

      const state = asyncPhaseReducer(pending, {
        type: "finish",
        scope: "decision:item-1",
        outcome,
        message: "Outcome recorded",
        correlationId: "corr-41",
      });

      expect(phaseFor(state, "decision:item-1")).toMatchObject({
        status,
        busy: false,
        message: "Outcome recorded",
        correlationId: "corr-41",
      });
    });

  it.each(["processing", "continue_prompt"] satisfies AsyncOutcome[])(
    "keeps %s processing without blocking an unrelated command",
    (outcome) => {
      const state = asyncPhaseReducer(createAsyncPhases(), {
        type: "finish",
        scope: "provider:1",
        outcome,
      });

      expect(phaseFor(state, "provider:1")).toEqual({ status: "processing", busy: true });
      expect(canBeginPhase(state, "provider:2")).toBe(true);
    },
  );

  it("deduplicates a second begin for the same pending scope", () => {
    const pending = asyncPhaseReducer(createAsyncPhases(), {
      type: "begin",
      scope: "decision:item-1",
    });

    expect(asyncPhaseReducer(pending, { type: "begin", scope: "decision:item-1" })).toBe(pending);
  });
});
