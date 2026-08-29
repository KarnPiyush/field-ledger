import { FieldLedgerClient } from "@fieldledger/api-client";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import { FieldLedgerDatabase } from "../data/database";
import { App } from "./App";

describe("mobile app async startup", () => {
  const databases: FieldLedgerDatabase[] = [];

  afterEach(async () => {
    await Promise.all(databases.map((db) => db.delete()));
  });

  it("clears loading and shows recovery when bootstrap and cached reads fail", async () => {
    const db = new FieldLedgerDatabase(`app-startup-${crypto.randomUUID()}`);
    databases.push(db);
    const initialize = vi.fn(async () => {
      throw new TypeError("API unavailable");
    });

    render(
      <MemoryRouter initialEntries={["/inbox"]}>
        <App db={db} initialize={initialize} />
      </MemoryRouter>,
    );

    expect(screen.getByText(/Loading saved field work/)).toBeVisible();
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Could not load FieldLedger data",
    );
    expect(screen.queryByText("Loading saved field work")).not.toBeInTheDocument();
    expect(initialize).toHaveBeenCalledOnce();
  });

  it("retries initialization when startup failed before the queue lifecycle existed", async () => {
    const db = new FieldLedgerDatabase(`app-startup-retry-${crypto.randomUUID()}`);
    databases.push(db);
    const initialize = vi
      .fn()
      .mockRejectedValueOnce(new TypeError("API unavailable"))
      .mockResolvedValueOnce({
        client: new FieldLedgerClient("https://mobile.fieldledger.test", "signed-session"),
        projectId: "project-1",
        clientId: "mobile:actor-1",
        fromCache: false,
      });

    render(
      <MemoryRouter initialEntries={["/inbox"]}>
        <App db={db} initialize={initialize} />
      </MemoryRouter>,
    );

    await screen.findByRole("alert");
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(await screen.findByText("No pending reviews")).toBeVisible();
    expect(initialize).toHaveBeenCalledTimes(2);
  });
});
