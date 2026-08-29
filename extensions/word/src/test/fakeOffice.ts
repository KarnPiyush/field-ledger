import type {
  OfficeAsyncResult,
  OfficeSettings,
} from "../office/storage";

export type SaveBehavior = "success" | "failure" | "timeout";

export class FakeOfficeSettings implements OfficeSettings {
  private staged: Map<string, unknown>;

  public constructor(
    private readonly durable = new Map<string, unknown>(),
    public behavior: SaveBehavior = "success",
  ) {
    this.staged = new Map(durable);
  }

  public get(name: string): unknown {
    return this.staged.get(name);
  }

  public set(name: string, value: unknown): void {
    this.staged.set(name, value);
  }

  public remove(name: string): void {
    this.staged.delete(name);
  }

  public saveAsync(callback: (result: OfficeAsyncResult) => void): void {
    if (this.behavior === "timeout") return;
    queueMicrotask(() => {
      if (this.behavior === "failure") {
        callback({ status: "failed", error: { message: "disk unavailable" } });
        return;
      }
      this.durable.clear();
      for (const [key, value] of this.staged) this.durable.set(key, value);
      callback({ status: "succeeded" });
    });
  }

  public restart(behavior: SaveBehavior = "success"): FakeOfficeSettings {
    return new FakeOfficeSettings(this.durable, behavior);
  }

  public committed(name: string): unknown {
    return this.durable.get(name);
  }
}
