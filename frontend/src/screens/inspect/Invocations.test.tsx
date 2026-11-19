import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AGENT_ID, fakeApi } from "../../test/fakeApi";
import { INV_ID, makeInvocation } from "../../test/inspectFixtures";
import { renderApp } from "../../test/render";
import { FakeWebSocket } from "../../test/setup";

const SECOND = "2".repeat(32);

describe("invocations", () => {
  it("lists invocations and opens a trace link", async () => {
    fakeApi((call) =>
      call.path.startsWith("/api/inspect/invocations?") ||
      call.path === "/api/inspect/invocations"
        ? { status: 200, body: { invocations: [makeInvocation()] } }
        : undefined,
    );
    renderApp("/inspect");
    const table = await screen.findByRole("table", { name: "Invocations" });
    expect(within(table).getByText("Test agent")).toBeInTheDocument();
    expect(within(table).getByText("agent_turn")).toBeInTheDocument();
    expect(within(table).getByText("$0.0021")).toBeInTheDocument();
    const link = within(table).getAllByRole("link")[0];
    expect(link).toHaveAttribute("href", `/inspect/invocations/${INV_ID}`);
  });

  it("sends the filters and the history search", async () => {
    const api = fakeApi((call) =>
      call.path.startsWith("/api/inspect/invocations")
        ? { status: 200, body: { invocations: [] } }
        : undefined,
    );
    renderApp("/inspect/invocations");
    await screen.findByText("No invocations.");
    await screen.findByRole("option", { name: "Test agent" });
    await userEvent.selectOptions(screen.getByLabelText("Agent"), AGENT_ID);
    await userEvent.selectOptions(screen.getByLabelText("Path"), "rule");
    await userEvent.selectOptions(screen.getByLabelText("Result"), "error");
    await userEvent.type(screen.getByLabelText("Search history"), "window");
    await userEvent.click(screen.getByRole("button", { name: "Search" }));
    await waitFor(() => {
      const last = api.calls
        .filter((c) => c.path.startsWith("/api/inspect/invocations"))
        .at(-1);
      expect(last?.path).toBe(
        `/api/inspect/invocations?agent_id=${AGENT_ID}&path=rule&status=error&q=window`,
      );
    });
  });

  it("updates the list live when a new invocation arrives", async () => {
    let rows = [makeInvocation()];
    fakeApi((call) =>
      call.path.startsWith("/api/inspect/invocations")
        ? { status: 200, body: { invocations: rows } }
        : undefined,
    );
    renderApp("/inspect/invocations");
    const table = await screen.findByRole("table", { name: "Invocations" });
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    const socket = FakeWebSocket.instances[0];
    if (!socket) throw new Error("no socket");
    act(() => {
      socket.emit({ topic: "events", action: "ready", id: "" });
    });
    rows = [makeInvocation({ id: SECOND, path: "rule" }), ...rows];
    act(() => {
      socket.emit({
        topic: "invocations",
        action: "finished",
        id: SECOND,
        data: { agent_id: AGENT_ID, status: "ok", path: "rule" },
      });
    });
    await waitFor(() => {
      expect(within(table).getAllByRole("row")).toHaveLength(3);
    });
    expect(within(table).getByText("rule")).toBeInTheDocument();
  });
});
