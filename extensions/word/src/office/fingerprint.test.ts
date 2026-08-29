import { describe, expect, it, vi } from "vitest";

import { fingerprintSnapshot } from "./fingerprint";

const base = {
  documentId: "document-1",
  bodyText: "Concrete  pour\r\nZone A",
  selectionText: "pour",
  selectionContext: "Concrete  pour in Zone A",
};

describe("Word document fingerprint", () => {
  it("is deterministic across pane restart, time, timezone, and equivalent whitespace", async () => {
    vi.setSystemTime("2026-01-01T00:00:00-08:00");
    const first = await fingerprintSnapshot(base);
    vi.setSystemTime("2038-07-14T22:45:00+09:00");
    const restarted = await fingerprintSnapshot({
      ...base,
      bodyText: "Concrete pour\nZone A",
      selectionContext: "Concrete pour in Zone A",
    });

    expect(restarted).toEqual(first);
  });

  it("changes the selection digest when the same selected words move to another anchor", async () => {
    const reviewed = await fingerprintSnapshot(base);
    const moved = await fingerprintSnapshot({
      ...base,
      selectionContext: "Zone B records include the Concrete pour",
    });

    expect(moved.selectionDigest).not.toBe(reviewed.selectionDigest);
    expect(moved.contentDigest).toBe(reviewed.contentDigest);
  });

  it("changes the content digest when normalized document content changes", async () => {
    const reviewed = await fingerprintSnapshot(base);
    const changed = await fingerprintSnapshot({
      ...base,
      bodyText: "Concrete pour\nZone B",
    });

    expect(changed.contentDigest).not.toBe(reviewed.contentDigest);
  });

  it("bounds and normalizes the selection preview", async () => {
    const result = await fingerprintSnapshot({
      ...base,
      selectionText: `  ${"anchor ".repeat(30)} `,
    });

    expect(result.selectionPreview.length).toBeLessThanOrEqual(96);
    expect(result.selectionPreview).not.toMatch(/\s{2}/);
  });
});
