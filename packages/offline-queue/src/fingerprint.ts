import type { DependencyKindDto } from "./types.js";

export interface DependencyFingerprintEntry {
  kind: DependencyKindDto;
  key: string;
  hash: string;
}

export type DigestAdapter = (canonical: string) => string | Promise<string>;

function compareText(left: string, right: string): number {
  const leftPoints = [...left].map((value) => value.codePointAt(0)!);
  const rightPoints = [...right].map((value) => value.codePointAt(0)!);
  for (let index = 0; index < Math.min(leftPoints.length, rightPoints.length); index += 1) {
    const difference = leftPoints[index]! - rightPoints[index]!;
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

function pythonJsonString(value: string): string {
  return JSON.stringify(value).replace(/[\u007f-\uffff]/g, (character) =>
    `\\u${character.charCodeAt(0).toString(16).padStart(4, "0")}`,
  );
}

export function canonicalDependencyJson(
  entries: readonly DependencyFingerprintEntry[],
): string {
  const sorted = [...entries].sort(
    (left, right) =>
      compareText(left.kind, right.kind) ||
      compareText(left.key, right.key) ||
      compareText(left.hash, right.hash),
  );
  const identities = new Set<string>();
  for (const entry of sorted) {
    const identity = `${entry.kind}\u0000${entry.key}`;
    if (identities.has(identity)) {
      throw new Error(`Duplicate dependency kind/key: ${entry.kind}/${entry.key}`);
    }
    identities.add(identity);
  }
  return `[${sorted
    .map(({ hash, key, kind }) =>
      `[${pythonJsonString(kind)},${pythonJsonString(key)},${pythonJsonString(hash)}]`,
    )
    .join(",")}]`;
}

function invalidJson(): never {
  throw new TypeError("Value is not canonical JSON");
}

function canonicalize(value: unknown, active: WeakSet<object>): string {
  if (value === null) return "null";
  if (typeof value === "string" || typeof value === "boolean") return JSON.stringify(value);
  if (typeof value === "number") {
    if (!Number.isFinite(value)) return invalidJson();
    return JSON.stringify(value);
  }
  if (typeof value !== "object") return invalidJson();
  if (active.has(value)) return invalidJson();
  active.add(value);

  let canonical: string;
  if (Array.isArray(value)) {
    const keys = Object.keys(value);
    if (
      keys.length !== value.length ||
      keys.some((key, index) => key !== String(index)) ||
      Object.getOwnPropertySymbols(value).length > 0
    ) {
      active.delete(value);
      return invalidJson();
    }
    canonical = `[${value.map((item) => canonicalize(item, active)).join(",")}]`;
  } else {
    const prototype = Object.getPrototypeOf(value);
    if (
      (prototype !== Object.prototype && prototype !== null) ||
      Object.getOwnPropertySymbols(value).length > 0
    ) {
      active.delete(value);
      return invalidJson();
    }
    const record = value as Record<string, unknown>;
    canonical = `{${Object.keys(record)
      .sort(compareText)
      .map((key) => `${JSON.stringify(key)}:${canonicalize(record[key], active)}`)
      .join(",")}}`;
  }

  active.delete(value);
  return canonical;
}

export function canonicalRequestJson(value: unknown): string {
  return canonicalize(value, new WeakSet<object>());
}

export async function digestDependencyVector(
  entries: readonly DependencyFingerprintEntry[],
  digest: DigestAdapter,
): Promise<string> {
  return digest(canonicalDependencyJson(entries));
}

export async function digestRequest(value: unknown, digest: DigestAdapter): Promise<string> {
  return digest(canonicalRequestJson(value));
}

export function makeIdempotencyKey(clientId: string, sequence: number): string {
  if (clientId.length === 0 || !Number.isSafeInteger(sequence) || sequence <= 0) {
    throw new RangeError("Idempotency sequence must be a positive safe integer");
  }
  return `${clientId}:${sequence}`;
}
