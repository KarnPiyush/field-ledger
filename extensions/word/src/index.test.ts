import { describe, expect, it } from "vitest";

import { workspace } from "./index.js";

describe("Word workspace", () => {
  it("exports its stable package identity", () => {
    expect(workspace).toBe("@fieldledger/word");
  });
});
