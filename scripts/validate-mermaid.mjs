import { readFile } from "node:fs/promises";

import { JSDOM } from "jsdom";

const input = process.argv[2];
if (!input) throw new Error("Expected a Mermaid source path");

const source = await readFile(input, "utf8");
const dom = new JSDOM("<!doctype html><html><body></body></html>", {
  pretendToBeVisual: true,
});
globalThis.window = dom.window;
globalThis.document = dom.window.document;

const { default: mermaid } = await import("mermaid");
mermaid.initialize({ securityLevel: "strict", startOnLoad: false });
await mermaid.parse(source, { suppressErrors: false });
dom.window.close();
