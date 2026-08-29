import { describe, expect, it, vi } from "vitest";

import type { QueueCommand } from "@fieldledger/offline-queue";

import type { ProposalGateway } from "../data/replay";
import type { WordInstruction } from "../office/bridge";
import { BrowserDemoProposalGateway, LocalWordBridge } from "./localBridge";

const command = {
  idempotencyKey: "word-demo-command-1",
} as QueueCommand<WordInstruction>;

describe("browser demo proposal gateway", () => {
  it("simulates a lost response after one real dispatch and reconciles without resubmitting", async () => {
    localStorage.clear();
    const bridge = new LocalWordBridge();
    const start = vi.fn().mockResolvedValue({ status: "PreviewReady", jobId: "job-1" });
    const reconcile = vi.fn().mockResolvedValue({ status: "PreviewReady", jobId: "job-1" });
    const inner: ProposalGateway = {
      start,
      reconcile,
      poll: vi.fn(),
    };
    const gateway = new BrowserDemoProposalGateway(bridge, inner);

    bridge.loseNextResponse();
    await expect(gateway.start(command)).rejects.toThrow("lost after dispatch");
    await expect(gateway.reconcile(command)).resolves.toMatchObject({ jobId: "job-1" });

    expect(start).toHaveBeenCalledTimes(1);
    expect(start).toHaveBeenCalledWith(command);
    expect(reconcile).toHaveBeenCalledTimes(1);
    expect(reconcile).toHaveBeenCalledWith(command);
  });
});
