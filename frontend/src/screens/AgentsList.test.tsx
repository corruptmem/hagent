import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AGENT_ID, fakeApi, makeAgent } from "../test/fakeApi";
import { renderApp } from "../test/render";

describe("agents list", () => {
  it("lists the agents and turns one off", async () => {
    const api = fakeApi((call) =>
      call.method === "PUT"
        ? { status: 200, body: makeAgent({ enabled: false }) }
        : undefined,
    );
    renderApp("/agents");
    const link = await screen.findByRole("link", { name: "Test agent" });
    expect(link).toHaveAttribute("href", `/agents/${AGENT_ID}`);
    await userEvent.click(screen.getByLabelText("Enabled: Test agent"));
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PUT",
        path: `/api/agents/${AGENT_ID}/enabled`,
        body: { enabled: false },
      });
    });
  });

  it("asks what to do with the private store before it deletes", async () => {
    const api = fakeApi((call) =>
      call.method === "DELETE"
        ? { status: 200, body: { deleted: true, private_store: "keep" } }
        : undefined,
    );
    renderApp("/agents");
    await userEvent.click(
      await screen.findByRole("button", { name: "Delete" }),
    );
    const dialog = screen.getByRole("dialog", { name: "Delete agent" });
    expect(dialog).toHaveTextContent("test_agent_private");
    expect(api.calls.some((c) => c.method === "DELETE")).toBe(false);
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete agent, keep store" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "DELETE",
        path: `/api/agents/${AGENT_ID}?private_store=keep`,
        body: undefined,
      });
    });
  });

  it("copies an agent and opens the copy", async () => {
    const copyId = "c".repeat(32);
    const api = fakeApi((call) => {
      if (call.method === "POST" && call.path.endsWith("/copy")) {
        return {
          status: 201,
          body: makeAgent({ id: copyId, name: "Test agent (copy)" }),
        };
      }
      if (call.path === `/api/agents/${copyId}`) {
        return {
          status: 200,
          body: makeAgent({ id: copyId, name: "Test agent (copy)" }),
        };
      }
      return undefined;
    });
    const { router } = renderApp("/agents");
    await userEvent.click(await screen.findByRole("button", { name: "Copy" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/agents/${copyId}`);
    });
    expect(
      api.calls.some((c) => c.path === `/api/agents/${AGENT_ID}/copy`),
    ).toBe(true);
  });
});
