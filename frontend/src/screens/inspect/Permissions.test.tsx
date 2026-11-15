import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Permissions } from "../../api/types";
import { AGENT_ID, fakeApi } from "../../test/fakeApi";
import { makeAudit } from "../../test/inspectFixtures";
import { renderApp } from "../../test/render";

function permissions(change: Partial<Permissions> = {}): Permissions {
  return {
    agent_id: AGENT_ID,
    version: 2,
    registry_stale: false,
    ha: {
      level: "control",
      services: [],
      outputs: [],
      read: {
        all: false,
        entities: ["light.test_lamp", "sensor.test_temperature"],
        domains: [],
      },
      control: { all: false, entities: ["light.test_lamp"], domains: [] },
    },
    tools: [
      { name: "ha.call_service", group: "ha" },
      { name: "ha.get_state", group: "ha" },
    ],
    knowledge: [],
    approval: { tools: [], services: ["light.turn_on"], entities: [] },
    denied: [
      makeAudit({
        decision: "denied",
        status: "denied",
        reason_code: "scope",
        reason: "lock.front is outside the read scope",
        request: { entity_id: "lock.front" },
      }),
    ],
    ...change,
  };
}

describe("permissions", () => {
  it("shows the effective scopes as entity IDs and the denied calls", async () => {
    const api = fakeApi((call) =>
      call.path === `/api/inspect/agents/${AGENT_ID}/permissions`
        ? { status: 200, body: permissions() }
        : undefined,
    );
    renderApp("/inspect/permissions");
    expect(
      await screen.findByText("Select an agent to see its permissions."),
    ).toBeInTheDocument();
    await screen.findByRole("option", { name: "Test agent" });
    await userEvent.selectOptions(screen.getByLabelText("Agent"), AGENT_ID);
    const read = await screen.findByLabelText("Read scope entities");
    expect(read).toHaveTextContent("sensor.test_temperature");
    expect(screen.getByLabelText("Control scope entities")).toHaveTextContent(
      "light.test_lamp",
    );
    expect(screen.getByLabelText("Tools")).toHaveTextContent("ha.get_state");
    const denied = screen.getByRole("table", { name: "Denied calls" });
    expect(
      within(denied).getByText(/outside the read scope/),
    ).toBeInTheDocument();
    expect(api.calls.some((c) => c.path.includes("/permissions"))).toBe(true);
  });

  it("says when the scope is admin or the registries are stale", async () => {
    fakeApi((call) =>
      call.path.endsWith("/permissions")
        ? {
            status: 200,
            body: permissions({
              registry_stale: true,
              ha: {
                level: "admin",
                services: [],
                outputs: [],
                read: { all: true, entities: [], domains: [] },
                control: { all: true, entities: [], domains: [] },
              },
              denied: [],
            }),
          }
        : undefined,
    );
    renderApp("/inspect/permissions");
    await screen.findByRole("option", { name: "Test agent" });
    await userEvent.selectOptions(screen.getByLabelText("Agent"), AGENT_ID);
    expect(
      await screen.findByText(/registries are not current/),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Every entity (admin).")).toHaveLength(2);
    expect(screen.getByText("No denied calls.")).toBeInTheDocument();
  });
});
