import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { AuditPage } from "./AuditPage";
import { InboxPage } from "./InboxPage";
import { OpsPage } from "./OpsPage";
import { QueuePage } from "./QueuePage";
import { ReviewPage } from "./ReviewPage";
import { samplePageModels } from "../test/fixtures";
import { StackedDiff } from "../components/StackedDiff";

function inRouter(ui: React.ReactNode) {
  return render(<MemoryRouter>{ui}</MemoryRouter>);
}

describe("review surfaces", () => {
  it("renders the cached inbox with operational metrics and one page heading", () => {
    inRouter(<InboxPage {...samplePageModels.inbox} />);

    expect(screen.getByRole("heading", { level: 1, name: "Review inbox" })).toBeVisible();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByText("7")).toBeVisible();
    expect(screen.getByText("Synced 8:52 AM")).toBeVisible();
    expect(screen.getByRole("link", { name: /Riverside Medical Center/ })).toHaveAttribute("href", "/review/batch-riverside/1");
  });

  it("offers explicit export and reset recovery for a corrupt saved queue", async () => {
    const user = userEvent.setup();
    const onExportQueue = vi.fn(async () => undefined);
    const onResetQueue = vi.fn(async () => undefined);
    inRouter(
      <InboxPage
        {...samplePageModels.inbox}
        error="The saved queue on this phone cannot be read."
        onExportQueue={onExportQueue}
        onResetQueue={onResetQueue}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Export queue" }));
    await user.click(screen.getByRole("button", { name: "Reset queue" }));

    expect(onExportQueue).toHaveBeenCalledOnce();
    expect(onResetQueue).toHaveBeenCalledOnce();
  });

  it("requires a visible rejection note and queues only one decision", async () => {
    const user = userEvent.setup();
    const onDecision = vi.fn(async () => undefined);
    inRouter(<ReviewPage {...samplePageModels.review} onDecision={onDecision} />);

    expect(screen.getByRole("img", { name: /Site Report 31, page 6/ })).toBeVisible();
    expect(screen.getByText("$42,500", { selector: "del" })).toBeVisible();
    expect(screen.getByText("$47,250", { selector: "ins" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.getByLabelText("Reason for rejection")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Save rejection" }));
    expect(screen.getByRole("alert")).toHaveTextContent("Add a reason before saving");
    expect(onDecision).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("Reason for rejection"), "The report does not support this amount.");
    await user.dblClick(screen.getByRole("button", { name: "Save rejection" }));
    expect(onDecision).toHaveBeenCalledTimes(1);
    expect(onDecision).toHaveBeenCalledWith("reject", "The report does not support this amount.");
  });

  it("renders proposed table changes as readable stacked tables", () => {
    render(
      <StackedDiff
        before="<table><tr><th>Item</th><th>Amount</th></tr><tr><td>Concrete</td><td>$42,500</td></tr></table>"
        after="<table><tr><th>Item</th><th>Amount</th></tr><tr><td>Concrete</td><td>$47,250</td></tr></table>"
        removed="$42,500"
        added="$47,250"
      />,
    );

    expect(screen.getByRole("table", { name: "Before table" })).toBeVisible();
    expect(screen.getByRole("table", { name: "After table" })).toBeVisible();
    expect(screen.getAllByRole("cell", { name: /Concrete/ })).toHaveLength(2);
    expect(screen.queryByText(/<table>/)).not.toBeInTheDocument();
  });

  it("does not claim a persistence failure was saved", async () => {
    const user = userEvent.setup();
    const onDecision = vi.fn(async () => { throw new Error("storage unavailable"); });
    inRouter(<ReviewPage {...samplePageModels.review} onDecision={onDecision} />);

    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("Could not save on this phone");
    expect(screen.queryByText("Saved on this phone")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("shows exact stale changes without an overwrite action", () => {
    inRouter(<QueuePage {...samplePageModels.queue} />);

    expect(screen.getByRole("heading", { level: 1, name: "Waiting to sync" })).toBeVisible();
    expect(screen.getByText("Nothing was applied")).toBeVisible();
    expect(screen.getByText(/Version 2/)).toBeVisible();
    expect(screen.getByText(/Version 3/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Refresh for review" })).toBeVisible();
    expect(screen.queryByRole("button", { name: /force|overwrite/i })).not.toBeInTheDocument();
    expect(screen.getByText("Waits for item 1")).toBeVisible();
  });

  it("identifies human actor, relay, lifecycle, and correlation in audit", () => {
    inRouter(<AuditPage {...samplePageModels.audit} />);

    expect(screen.getByRole("heading", { level: 1, name: "Audit trail" })).toBeVisible();
    expect(screen.getByText("Maya Chen")).toBeVisible();
    expect(screen.getByText("Phone via FieldLedger relay")).toBeVisible();
    expect(screen.getByText("corr-014-41")).toBeVisible();
  });

  it.each(["Processing", "Review ready", "Completed", "Failed", "Cancelled", "Needs reconciliation"])(
    "renders the %s provider job state and scoped command",
    (state) => {
      inRouter(<OpsPage {...samplePageModels.ops} />);
      expect(screen.getByText(state)).toBeVisible();
    },
  );
});
