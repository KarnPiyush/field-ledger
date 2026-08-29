import { describe, expect, it } from "vitest";

import {
  asyncPhaseReducer,
  createAsyncPhases,
  phaseFor,
  type AsyncOutcome,
} from "./asyncPhase";

describe("Word async phase state", () => {
  it("clears stale status text whenever a new phase begins", () => {
    const prior = asyncPhaseReducer(createAsyncPhases(), {
      type: "finish",
      scope: "queue:1",
      outcome: "completed",
      message: "Saved in this document",
    });

    const next = asyncPhaseReducer(prior, { type: "begin", scope: "queue:1" });

    expect(phaseFor(next, "queue:1")).toEqual({ status: "pending", busy: true });
  });

  it.each([
    ["review_ready", "review"],
    ["preview_ready", "review"],
    ["PreviewReady", "review"],
    ["awaiting_approval", "review"],
    ["completed", "success"],
    ["applied", "success"],
    ["stale", "attention"],
    ["failed", "error"],
    ["cancelled", "error"],
    ["timeout", "error"],
    ["fetch_error", "error"],
    ["stream_error", "error"],
    ["post_refresh_error", "error"],
    ["storage_error", "error"],
    ["reconciliation_required", "reconcile"],
    ["lost_response", "reconcile"],
  ] satisfies [AsyncOutcome, string][]) (
    "maps %s to a non-loading %s phase",
    (outcome, status) => {
      const next = asyncPhaseReducer(createAsyncPhases(), {
        type: "finish",
        scope: "submit",
        outcome,
      });

      expect(phaseFor(next, "submit")).toMatchObject({ status, busy: false });
    },
  );

  it.each(["processing", "continue_prompt"] satisfies AsyncOutcome[])(
    "keeps %s processing until another outcome arrives",
    (outcome) => {
      const next = asyncPhaseReducer(createAsyncPhases(), {
        type: "finish",
        scope: "submit",
        outcome,
      });

      expect(phaseFor(next, "submit")).toEqual({ status: "processing", busy: true });
    },
  );
});
