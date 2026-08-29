import { encodeQueue, type Sender, type SendResult } from "@fieldledger/offline-queue";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import type {
  DocumentFingerprint,
  SerializedQueue,
  WordBridge,
  WordInstruction,
} from "../office/bridge";
import { Taskpane } from "./Taskpane";

const fingerprint: DocumentFingerprint = {
  documentId: "document-1",
  contentDigest: "content-1",
  selectionDigest: "selection-1",
  selectionPreview: "north annex",
};

class PaneBridge implements WordBridge {
  public serialized: SerializedQueue = encodeQueue([]);
  public saveError: Error | undefined;
  public saveGate: Promise<void> | undefined;
  public opened: string[] = [];

  public async ready(): Promise<void> {}
  public async getFingerprint(): Promise<DocumentFingerprint> {
    return fingerprint;
  }
  public async loadQueue(): Promise<SerializedQueue> {
    return this.serialized;
  }
  public async saveQueue(queue: SerializedQueue): Promise<void> {
    if (this.saveGate) await this.saveGate;
    if (this.saveError) throw this.saveError;
    this.serialized = queue;
  }
  public async openReview(url: string): Promise<void> {
    this.opened.push(url);
  }
}

class PaneSender implements Sender<WordInstruction> {
  public async send(): Promise<SendResult> {
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
  public reconcile = this.send;
}

describe("Word task pane", () => {
  it("renders the actual 330px-first composer as the first screen", async () => {
    render(
      <Taskpane
        bridge={new PaneBridge()}
        sender={new PaneSender()}
        online={false}
        hostLabel="Browser demo"
      />,
    );

    expect(await screen.findByRole("heading", { level: 1, name: "FieldLedger" })).toBeVisible();
    expect(screen.getByText("Browser demo")).toBeVisible();
    expect(screen.getByLabelText("Instruction")).toBeVisible();
    expect(screen.getByRole("radiogroup", { name: "Instruction target" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Queue command" })).toBeDisabled();
    expect(screen.getByText("Offline")).toBeVisible();
  });

  it("saves an offline instruction, clears the composer, and restores it after pane restart", async () => {
    const user = userEvent.setup();
    const bridge = new PaneBridge();
    const first = render(
      <Taskpane bridge={bridge} sender={new PaneSender()} online={false} hostLabel="Word" />,
    );
    const textarea = await screen.findByLabelText("Instruction");

    await user.type(textarea, "Update the concrete quantity table");
    await user.click(screen.getByRole("button", { name: "Queue command" }));

    expect(await screen.findByText("Saved in this document")).toBeVisible();
    expect(textarea).toHaveValue("");
    expect(screen.getByText("Waiting to sync")).toBeVisible();
    first.unmount();

    render(<Taskpane bridge={bridge} sender={new PaneSender()} online={false} hostLabel="Word" />);
    expect(await screen.findByText("Update the concrete quantity table")).toBeVisible();
  });

  it("retains composer text and says it was not saved after a settings failure", async () => {
    const user = userEvent.setup();
    const bridge = new PaneBridge();
    const log = vi.spyOn(console, "error").mockImplementation(() => undefined);
    bridge.saveError = new Error("saveAsync failed");
    render(<Taskpane bridge={bridge} sender={new PaneSender()} online={false} hostLabel="Word" />);
    const textarea = await screen.findByLabelText("Instruction");

    await user.type(textarea, "Keep this instruction");
    await user.click(screen.getByRole("button", { name: "Queue command" }));

    expect(await screen.findByText(/was not saved/i)).toBeVisible();
    expect(textarea).toHaveValue("Keep this instruction");
    expect(screen.getByRole("button", { name: "Queue command" })).toBeEnabled();
    expect(log).toHaveBeenCalledWith("Word instruction save failed", expect.any(Error));
    log.mockRestore();
  });

  it("coalesces a double submit into one durable queue row", async () => {
    const user = userEvent.setup();
    const bridge = new PaneBridge();
    let release!: () => void;
    bridge.saveGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    render(<Taskpane bridge={bridge} sender={new PaneSender()} online={false} hostLabel="Word" />);
    await user.type(await screen.findByLabelText("Instruction"), "One instruction");
    const button = screen.getByRole("button", { name: "Queue command" });

    await user.dblClick(button);
    release();

    await waitFor(() => expect(screen.getAllByText("One instruction")).toHaveLength(1));
  });

  it("blocks on corrupt document queue data with export and reset recovery", async () => {
    const bridge = new PaneBridge();
    bridge.serialized = '{"schemaVersion":99,"commands":[]}';
    render(<Taskpane bridge={bridge} sender={new PaneSender()} online={false} hostLabel="Word" />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/cannot be read/i);
    expect(screen.getByRole("button", { name: "Export queue" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Reset queue" })).toBeVisible();
    expect(screen.getByRole("button", { name: "Queue command" })).toBeDisabled();
  });

  it("exposes review-ready completion without leaving the command loading", async () => {
    const user = userEvent.setup();
    const bridge = new PaneBridge();
    const appliedResult: SendResult = {
      kind: "applied",
      response: { batch_id: "batch-1", status: "preview_ready" },
    };
    const sender: Sender<WordInstruction> = {
      send: vi.fn(async () => appliedResult),
      reconcile: vi.fn(async () => appliedResult),
    };
    render(
      <Taskpane
        bridge={bridge}
        sender={sender}
        online
        hostLabel="Word"
        reviewUrlForBatch={(batchId) => `https://fieldledger.test/reviews/${batchId}`}
      />,
    );
    await user.type(await screen.findByLabelText("Instruction"), "Prepare a proposal");

    await user.click(screen.getByRole("button", { name: "Queue command" }));

    const open = await screen.findByRole("button", { name: "Open proposal review" });
    await user.type(screen.getByLabelText("Instruction"), "Next instruction");
    expect(screen.getByRole("button", { name: "Queue command" })).toBeEnabled();
    await user.click(open);
    expect(bridge.opened).toEqual(["https://fieldledger.test/reviews/batch-1"]);
  });

  it("clears the prior result as soon as a retry phase begins", async () => {
    const user = userEvent.setup();
    const bridge = new PaneBridge();
    const failed: SendResult = {
      kind: "failed",
      problem: {
        kind: "failed",
        code: "provider-failed",
        title: "Provider failed",
        detail: "The prior attempt failed.",
      },
    };
    const sender: Sender<WordInstruction> = {
      send: vi.fn(async () => failed),
      reconcile: vi.fn(async () => failed),
    };
    render(<Taskpane bridge={bridge} sender={sender} online hostLabel="Word" />);
    await user.type(await screen.findByLabelText("Instruction"), "Retry this instruction");
    await user.click(screen.getByRole("button", { name: "Queue command" }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Nothing was applied"),
    );

    let release!: () => void;
    bridge.saveGate = new Promise<void>((resolve) => {
      release = resolve;
    });
    await user.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("Checking queue"),
    );
    release();
  });
});
