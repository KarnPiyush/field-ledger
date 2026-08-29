import { encodeQueue } from "@fieldledger/offline-queue";
import { describe, expect, it } from "vitest";

import { FakeOfficeSettings } from "../test/fakeOffice";
import {
  DOCUMENT_ID_KEY,
  OfficePersistenceError,
  OfficeSettingsStorage,
  QUEUE_KEY,
} from "./storage";

describe("Office settings storage", () => {
  it("persists one namespaced serialized queue value and restores it after restart", async () => {
    const office = new FakeOfficeSettings();
    const storage = new OfficeSettingsStorage(office);
    const serialized = encodeQueue([]);

    await storage.saveQueue(serialized);

    expect(office.committed(QUEUE_KEY)).toBe(serialized);
    await expect(new OfficeSettingsStorage(office.restart()).loadQueue()).resolves.toBe(serialized);
  });

  it("rejects a failed saveAsync and does not expose the uncommitted queue after restart", async () => {
    const office = new FakeOfficeSettings(undefined, "failure");
    const storage = new OfficeSettingsStorage(office);

    await expect(storage.saveQueue(encodeQueue([]))).rejects.toMatchObject({
      name: "OfficePersistenceError",
      code: "save_failed",
    });
    await expect(new OfficeSettingsStorage(office.restart()).loadQueue()).resolves.toBe(
      encodeQueue([]),
    );
  });

  it("rolls back a staged queue after save failure so retry persists the command", async () => {
    const office = new FakeOfficeSettings(undefined, "failure");
    const storage = new OfficeSettingsStorage(office);
    const serialized = '{"schemaVersion":1,"commands":[]}';

    await expect(storage.saveQueue(serialized)).rejects.toMatchObject({ code: "save_failed" });
    expect(office.get(QUEUE_KEY)).toBeUndefined();

    office.behavior = "success";
    await storage.saveQueue(serialized);

    await expect(new OfficeSettingsStorage(office.restart()).loadQueue()).resolves.toBe(serialized);
  });

  it("rejects when saveAsync never completes", async () => {
    const storage = new OfficeSettingsStorage(
      new FakeOfficeSettings(undefined, "timeout"),
      { timeoutMs: 10 },
    );

    await expect(storage.saveQueue(encodeQueue([]))).rejects.toEqual(
      expect.objectContaining<Partial<OfficePersistenceError>>({ code: "save_timeout" }),
    );
  });

  it("does not return a first-use document id until saveAsync commits it", async () => {
    const office = new FakeOfficeSettings();
    const storage = new OfficeSettingsStorage(office, {
      createId: () => "document-durable-id",
    });

    await expect(storage.getOrCreateDocumentId()).resolves.toBe("document-durable-id");
    expect(office.committed(DOCUMENT_ID_KEY)).toBe("document-durable-id");
    await expect(
      new OfficeSettingsStorage(office.restart(), {
        createId: () => "must-not-replace",
      }).getOrCreateDocumentId(),
    ).resolves.toBe("document-durable-id");
  });

  it("propagates first-use document id persistence failure", async () => {
    const office = new FakeOfficeSettings(undefined, "failure");
    const storage = new OfficeSettingsStorage(
      office,
      { createId: () => "uncommitted-id" },
    );

    await expect(storage.getOrCreateDocumentId()).rejects.toMatchObject({ code: "save_failed" });
    expect(office.get(DOCUMENT_ID_KEY)).toBeUndefined();

    office.behavior = "success";
    await expect(storage.getOrCreateDocumentId()).resolves.toBe("uncommitted-id");
    expect(office.committed(DOCUMENT_ID_KEY)).toBe("uncommitted-id");
  });
});
