import { describe, expect, it, vi } from "vitest";

import { registerQueueLifecycle } from "./lifecycle";

describe("queue lifecycle drain", () => {
  it("drains on app start, online resume, visibility resume, and explicit retry", async () => {
    const drain = vi.fn(async () => undefined);
    const lifecycle = registerQueueLifecycle(drain);

    await vi.waitFor(() => expect(drain).toHaveBeenCalledTimes(1));
    window.dispatchEvent(new Event("online"));
    await vi.waitFor(() => expect(drain).toHaveBeenCalledTimes(2));
    Object.defineProperty(document, "visibilityState", { configurable: true, value: "visible" });
    document.dispatchEvent(new Event("visibilitychange"));
    await vi.waitFor(() => expect(drain).toHaveBeenCalledTimes(3));
    await lifecycle.retry();

    await vi.waitFor(() => expect(drain).toHaveBeenCalledTimes(4));
    lifecycle.dispose();
    window.dispatchEvent(new Event("online"));
    expect(drain).toHaveBeenCalledTimes(4);
  });

  it("coalesces overlapping lifecycle requests", async () => {
    let resolve!: () => void;
    const drain = vi.fn(() => new Promise<void>((done) => { resolve = done; }));
    const lifecycle = registerQueueLifecycle(drain);

    window.dispatchEvent(new Event("online"));
    document.dispatchEvent(new Event("visibilitychange"));
    expect(drain).toHaveBeenCalledTimes(1);
    resolve();
    const retry = lifecycle.retry();
    await vi.waitFor(() => expect(drain).toHaveBeenCalledTimes(2));
    resolve();
    await retry;
    expect(drain).toHaveBeenCalledTimes(2);
    lifecycle.dispose();
  });
});
