import { describe, expect, it, vi } from "vitest";

import { FakeOfficeSettings } from "../test/fakeOffice";
import { OfficeWordBridge } from "./bridge";

describe("Office Word bridge", () => {
  it("waits for Office ready and builds a fingerprint from read-only document data", async () => {
    const ready = vi.fn(async () => undefined);
    const readSnapshot = vi.fn(async () => ({
      bodyText: "North Annex specification",
      selectionText: "Annex",
      selectionContext: "North Annex specification",
    }));
    const bridge = new OfficeWordBridge({
      ready,
      settings: new FakeOfficeSettings(),
      readSnapshot,
      openBrowserWindow: vi.fn(),
      createId: () => "word-document-1",
    });

    await bridge.ready();
    await expect(bridge.getFingerprint()).resolves.toMatchObject({
      documentId: "word-document-1",
      selectionPreview: "Annex",
    });
    expect(ready).toHaveBeenCalledOnce();
    expect(readSnapshot).toHaveBeenCalledOnce();
  });

  it("opens proposal review through the Office browser boundary", async () => {
    const openBrowserWindow = vi.fn();
    const bridge = new OfficeWordBridge({
      ready: async () => undefined,
      settings: new FakeOfficeSettings(),
      readSnapshot: async () => ({ bodyText: "", selectionText: "", selectionContext: "" }),
      openBrowserWindow,
    });

    await bridge.openReview("https://fieldledger.test/reviews/batch-1");

    expect(openBrowserWindow).toHaveBeenCalledWith("https://fieldledger.test/reviews/batch-1");
  });
});
