import type { DocumentFingerprint } from "./bridge";

export interface DocumentSnapshot {
  documentId: string;
  bodyText: string;
  selectionText: string;
  selectionContext: string;
}

export function normalizeDocumentText(value: string): string {
  return value.normalize("NFC").replace(/\s+/gu, " ").trim();
}

export async function sha256Text(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)]
    .map((part) => part.toString(16).padStart(2, "0"))
    .join("");
}

function preview(value: string): string {
  const normalized = normalizeDocumentText(value);
  return normalized.length <= 96 ? normalized : `${normalized.slice(0, 93)}...`;
}

export async function fingerprintSnapshot(
  snapshot: DocumentSnapshot,
): Promise<DocumentFingerprint> {
  const body = normalizeDocumentText(snapshot.bodyText);
  const selected = normalizeDocumentText(snapshot.selectionText);
  const context = normalizeDocumentText(snapshot.selectionContext).slice(0, 512);
  const [contentDigest, selectionDigest] = await Promise.all([
    sha256Text(body),
    sha256Text(JSON.stringify({ selected, context })),
  ]);
  return {
    documentId: snapshot.documentId,
    contentDigest,
    selectionDigest,
    selectionPreview: preview(selected),
  };
}
