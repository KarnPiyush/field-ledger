import fc from "fast-check";
import { describe, expect, it, vi } from "vitest";

import {
  canonicalDependencyJson,
  canonicalRequestJson,
  digestDependencyVector,
  digestRequest,
  makeIdempotencyKey,
  type DependencyFingerprintEntry,
} from "./index.js";

const kinds = [
  "target_document_version",
  "source_version",
  "rule_set_version",
  "policy_version",
  "proposal",
  "workflow_artifact",
] as const;

describe("dependency fingerprints", () => {
  it("uses a literal canonical kind/key/hash representation", () => {
    expect(
      canonicalDependencyJson([
        { kind: "source_version", key: "b", hash: "hash-2" },
        { kind: "policy_version", key: "a", hash: "hash-1" },
      ]),
    ).toBe(
      '[["policy_version","a","hash-1"],["source_version","b","hash-2"]]',
    );
  });

  it("is independent of dependency input order", () => {
    const entry = fc.record({
      kind: fc.constantFrom(...kinds),
      key: fc.string({ minLength: 1 }),
      hash: fc.string(),
    });

    fc.assert(
      fc.property(fc.uniqueArray(entry, { selector: (item) => `${item.kind}:${item.key}` }), (entries) => {
        expect(canonicalDependencyJson(entries)).toBe(
          canonicalDependencyJson([...entries].reverse()),
        );
      }),
      { seed: 20260823, numRuns: 200 },
    );
  });

  it("allows the same key in different dependency kinds", () => {
    expect(
      canonicalDependencyJson([
        { kind: "source_version", key: "same", hash: "one" },
        { kind: "proposal", key: "same", hash: "two" },
      ]),
    ).toBe('[["proposal","same","two"],["source_version","same","one"]]');
  });

  it("orders same-kind entries by key and still rejects same-key entries after hash comparison", () => {
    expect(
      canonicalDependencyJson([
        { kind: "source_version", key: "b", hash: "two" },
        { kind: "source_version", key: "a", hash: "one" },
      ]),
    ).toBe(
      '[["source_version","a","one"],["source_version","b","two"]]',
    );
    expect(() =>
      canonicalDependencyJson([
        { kind: "source_version", key: "same", hash: "two" },
        { kind: "source_version", key: "same", hash: "one" },
      ]),
    ).toThrow("Duplicate dependency kind/key: source_version/same");
  });

  it("matches the Python dependency-vector bytes and sha256 digest", async () => {
    const canonical = canonicalDependencyJson([
      { kind: "source_version", key: "b", hash: "hash-2" },
      { kind: "policy_version", key: "a", hash: "hash-1" },
    ]);
    const sha256 = async (value: string) => {
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
      return `sha256:${[...new Uint8Array(digest)]
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("")}`;
    };

    expect(canonical).toBe(
      '[["policy_version","a","hash-1"],["source_version","b","hash-2"]]',
    );
    await expect(digestDependencyVector([
      { kind: "source_version", key: "b", hash: "hash-2" },
      { kind: "policy_version", key: "a", hash: "hash-1" },
    ], sha256)).resolves.toBe(
      "sha256:948e5132ddc3e1e2bee243e16ed1d5239af804a7f63967d865c0a288da8d4ce3",
    );
  });

  it("matches Python ensure_ascii bytes for Unicode dependency text", async () => {
    const entries = [
      { kind: "source_version" as const, key: "résumé/📄", hash: "sha256:é" },
    ];
    const canonical = canonicalDependencyJson(entries);
    const sha256 = async (value: string) => {
      const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
      return `sha256:${[...new Uint8Array(digest)]
        .map((byte) => byte.toString(16).padStart(2, "0"))
        .join("")}`;
    };

    expect(canonical).toBe(
      '[["source_version","r\\u00e9sum\\u00e9/\\ud83d\\udcc4","sha256:\\u00e9"]]',
    );
    await expect(digestDependencyVector(entries, sha256)).resolves.toBe(
      "sha256:1a905f29a38e787b8d45781b928865b84cd6addafe0f7967a904fd9a32c720f8",
    );
  });

  it("injects the digest implementation and hashes only canonical text", async () => {
    const hash = vi.fn(async (canonical: string) => `digest:${canonical.length}`);
    const entries: readonly DependencyFingerprintEntry[] = [
      { kind: "proposal", key: "proposal:1", hash: "sha256:value" },
    ];

    await expect(digestDependencyVector(entries, hash)).resolves.toBe("digest:42");
    expect(hash).toHaveBeenCalledWith(
      '[["proposal","proposal:1","sha256:value"]]',
    );
  });
});

describe("request fingerprints", () => {
  it("sorts nested object keys while retaining array order", () => {
    expect(canonicalRequestJson(null)).toBe("null");
    expect(canonicalRequestJson({ z: 1, a: { y: true, x: [2, 1] } })).toBe(
      '{"a":{"x":[2,1],"y":true},"z":1}',
    );
  });

  it("is independent of object insertion order", () => {
    fc.assert(
      fc.property(fc.dictionary(fc.string({ minLength: 1 }), fc.integer()), (record) => {
        const reversed = Object.fromEntries(Object.entries(record).reverse());
        expect(canonicalRequestJson(record)).toBe(canonicalRequestJson(reversed));
      }),
      { seed: 20260824, numRuns: 200 },
    );
  });

  it.each([
    undefined,
    { missing: undefined },
    Number.NaN,
    Number.POSITIVE_INFINITY,
    Number.NEGATIVE_INFINITY,
    1n,
    () => undefined,
    Symbol("nope"),
    new Date("2026-01-01T00:00:00Z"),
    Object.assign([1, 2], { extra: true }),
  ])("rejects non-JSON value %#", (value) => {
    expect(() => canonicalRequestJson(value)).toThrow("Value is not canonical JSON");
  });

  it("rejects sparse and cyclic structures", () => {
    const sparse = new Array(2);
    const cyclic: { self?: unknown } = {};
    cyclic.self = cyclic;

    expect(() => canonicalRequestJson(sparse)).toThrow("Value is not canonical JSON");
    expect(() => canonicalRequestJson(cyclic)).toThrow("Value is not canonical JSON");
  });

  it("normalizes negative zero and invokes the request digest adapter", async () => {
    const hash = vi.fn((canonical: string) => `hash:${canonical}`);

    expect(canonicalRequestJson(-0)).toBe("0");
    await expect(digestRequest({ b: 2, a: 1 }, hash)).resolves.toBe(
      'hash:{"a":1,"b":2}',
    );
  });
});

describe("idempotency keys", () => {
  it("uses the stable client and allocated sequence", () => {
    expect(makeIdempotencyKey("phone-7", 42)).toBe("phone-7:42");
    expect(makeIdempotencyKey("phone-7", Number.MAX_SAFE_INTEGER)).toBe(
      `phone-7:${Number.MAX_SAFE_INTEGER}`,
    );
  });

  it.each([
    ["", 1],
    ["phone", 0],
    ["phone", -1],
    ["phone", 1.5],
    ["phone", Number.POSITIVE_INFINITY],
    ["phone", Number.MAX_SAFE_INTEGER + 1],
  ])("rejects invalid client/sequence %#", (clientId, sequence) => {
    expect(() => makeIdempotencyKey(clientId, sequence)).toThrow(
      "Idempotency sequence must be a positive safe integer",
    );
  });
});
