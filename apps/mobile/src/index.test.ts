import { describe, expect, it } from "vitest";

import { workspace } from "./index.js";

describe("mobile workspace", () => {
  it("exports its stable package identity", () => {
    expect(workspace).toBe("@fieldledger/mobile");
  });
});
