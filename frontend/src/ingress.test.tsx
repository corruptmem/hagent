import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AGENT_ID, fakeApi } from "./test/fakeApi";
import { renderApp } from "./test/render";
import { FakeWebSocket } from "./test/setup";

const BASE = "/api/hassio_ingress/abc123";

describe("the UI under the Ingress base path", () => {
  it("routes, links and calls the API under the base path", async () => {
    const element = document.createElement("base");
    element.href = `${BASE}/`;
    document.head.prepend(element);
    const api = fakeApi(undefined, BASE);
    renderApp("/agents", BASE);
    const link = await screen.findByRole("link", { name: "Test agent" });
    expect(link).toHaveAttribute("href", `${BASE}/agents/${AGENT_ID}`);
    expect(api.calls[0]?.path).toBe(`${BASE}/api/agents`);
    expect(FakeWebSocket.instances[0]?.url).toMatch(
      new RegExp(`${BASE}/api/events$`),
    );
  });

  it("loads the agents again after a change event", async () => {
    const api = fakeApi();
    renderApp("/agents");
    await screen.findByRole("link", { name: "Test agent" });
    const socket = FakeWebSocket.instances[0];
    socket?.emit({ topic: "events", action: "ready", id: "" });
    const before = api.calls.filter((c) => c.path === "/api/agents").length;
    socket?.emit({
      topic: "agents",
      action: "updated",
      id: AGENT_ID,
      data: {},
    });
    await waitFor(() => {
      expect(api.calls.filter((c) => c.path === "/api/agents").length).toBe(
        before + 1,
      );
    });
  });
});
