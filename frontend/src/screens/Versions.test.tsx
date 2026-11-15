import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AGENT_ID, DEFAULTS, fakeApi, makeAgent } from "../test/fakeApi";
import { renderApp } from "../test/render";

describe("versions", () => {
  it("compares two versions", async () => {
    const api = fakeApi((call) =>
      call.path.startsWith(`/api/agents/${AGENT_ID}/diff`)
        ? {
            status: 200,
            body: {
              from: 1,
              to: 2,
              changes: [{ path: "budget.daily_usd", old: 0.5, new: 2 }],
            },
          }
        : undefined,
    );
    renderApp(`/agents/${AGENT_ID}/versions`);
    const changes = await screen.findByRole("table", { name: "Changes" });
    expect(within(changes).getByText("budget.daily_usd")).toBeInTheDocument();
    expect(within(changes).getByText("0.5")).toBeInTheDocument();
    expect(
      api.calls.some(
        (c) => c.path === `/api/agents/${AGENT_ID}/diff?from=1&to=2`,
      ),
    ).toBe(true);
  });

  it("shows a version and restores it", async () => {
    const api = fakeApi((call) => {
      if (call.path === `/api/agents/${AGENT_ID}/versions/1`) {
        return {
          status: 200,
          body: {
            version: 1,
            change: "create",
            source_agent_id: null,
            source_version: null,
            created_at: "2026-11-14T10:00:00+00:00",
            definition: { ...DEFAULTS, name: "Old name" },
          },
        };
      }
      if (call.method === "POST" && call.path.endsWith("/restore")) {
        return { status: 200, body: makeAgent({ version: 3 }) };
      }
      if (call.path.includes("/diff")) {
        return { status: 200, body: { from: 1, to: 2, changes: [] } };
      }
      return undefined;
    });
    renderApp(`/agents/${AGENT_ID}/versions`);
    // Click as soon as the table shows. The cells must stay mounted while
    // the other queries load, or the click goes to a removed element.
    const viewButtons = await screen.findAllByRole("button", { name: "View" });
    await userEvent.click(viewButtons[1] as HTMLElement);
    expect(
      await screen.findByLabelText("Definition of version 1"),
    ).toHaveTextContent("Old name");
    await userEvent.click(screen.getByRole("button", { name: "Restore" }));
    const dialog = screen.getByRole("dialog", { name: "Restore version" });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Restore version 1" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "POST",
        path: `/api/agents/${AGENT_ID}/versions/1/restore`,
        body: { base_version: 2 },
      });
    });
  });
});
