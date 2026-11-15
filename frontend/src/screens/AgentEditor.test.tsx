import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { AgentDefinition } from "../api/types";
import { leafPaths } from "../definition";
import { AGENT_ID, DEFAULTS, fakeApi, makeAgent } from "../test/fakeApi";
import { renderApp } from "../test/render";

function putBody(calls: { method: string; body: unknown }[]) {
  const put = calls.find((c) => c.method === "PUT");
  return put?.body as
    { definition: AgentDefinition; base_version: number } | undefined;
}

describe("agent editor", () => {
  it("has a control for every definition field", async () => {
    fakeApi();
    const { container } = renderApp(`/agents/${AGENT_ID}`);
    await screen.findByDisplayValue("Test agent");
    const paths = leafPaths(DEFAULTS);
    // The fixture is the output of GET api/agents/defaults (a Python test
    // keeps it equal), so this list holds every field of section 7.1.
    expect(paths).toContain("tools.ha.scope.labels");
    expect(paths).toContain("review.cron");
    const missing = paths.filter(
      (p) => !container.querySelector(`[data-path="${p}"]`),
    );
    expect(missing).toEqual([]);
  });

  it("picks entities from the HA catalogue and saves with the base version", async () => {
    const api = fakeApi((call) =>
      call.method === "PUT"
        ? { status: 200, body: makeAgent({ version: 3 }) }
        : undefined,
    );
    renderApp(`/agents/${AGENT_ID}`);
    const search = await screen.findByLabelText(/^Outputs .*: search$/);
    await userEvent.type(search, "lamp");
    const matches = await screen.findByRole("listbox", {
      name: /Outputs .*: matches/,
    });
    await userEvent.click(
      within(matches).getByRole("button", { name: /Test lamp/ }),
    );
    await userEvent.type(
      screen.getByLabelText("Input devices: search"),
      "lamp dev",
    );
    await userEvent.click(
      await screen.findByRole("button", { name: /Lamp device/ }),
    );
    await userEvent.selectOptions(
      screen.getByLabelText("HA access level"),
      "control",
    );
    await userEvent.type(screen.getByLabelText("Scope: areas: search"), "room");
    await userEvent.click(
      await screen.findByRole("button", { name: /Room A/ }),
    );
    const budget = screen.getByLabelText("Daily budget (USD)");
    await userEvent.clear(budget);
    await userEvent.type(budget, "2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(putBody(api.calls)).toBeDefined();
    });
    const body = putBody(api.calls);
    expect(body?.base_version).toBe(2);
    expect(body?.definition.outputs).toEqual(["light.test_lamp"]);
    expect(body?.definition.inputs.devices).toEqual(["dev1"]);
    expect(body?.definition.tools.ha.level).toBe("control");
    expect(body?.definition.tools.ha.scope.areas).toEqual(["area1"]);
    expect(body?.definition.budget.daily_usd).toBe(2);
  });

  it("accepts an entity ID that HA does not list", async () => {
    const api = fakeApi((call) =>
      call.method === "PUT" ? { status: 200, body: makeAgent() } : undefined,
    );
    renderApp(`/agents/${AGENT_ID}`);
    const search = await screen.findByLabelText("Input entities: search");
    await userEvent.type(search, "input_boolean.test{Enter}");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => {
      expect(putBody(api.calls)?.definition.inputs.entities).toEqual([
        "input_boolean.test",
      ]);
    });
  });

  it("shows validation errors next to the fields", async () => {
    fakeApi((call) =>
      call.method === "PUT"
        ? {
            status: 422,
            body: {
              error: {
                code: "definition_invalid",
                message: "the definition is not valid",
                details: [
                  {
                    loc: ["budget", "daily_usd"],
                    msg: "Input should be less than or equal to 1000",
                    type: "less_than_equal",
                  },
                ],
              },
            },
          }
        : undefined,
    );
    renderApp(`/agents/${AGENT_ID}`);
    const budget = await screen.findByLabelText("Daily budget (USD)");
    await userEvent.clear(budget);
    await userEvent.type(budget, "5000");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    const alerts = await screen.findAllByText(/less than or equal to 1000/);
    expect(alerts.length).toBe(2);
    expect(budget.closest(".field")).toHaveClass("invalid");
  });

  it("reports a version conflict and loads the current version", async () => {
    let conflict = true;
    const api = fakeApi((call) => {
      if (call.method === "PUT" && conflict) {
        conflict = false;
        return {
          status: 409,
          body: {
            error: {
              code: "version_conflict",
              message: "the agent changed",
              details: { current_version: 3 },
            },
          },
        };
      }
      return undefined;
    });
    renderApp(`/agents/${AGENT_ID}`);
    const name = await screen.findByDisplayValue("Test agent");
    await userEvent.type(name, " 2");
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await userEvent.click(
      await screen.findByRole("button", { name: "Load the current version" }),
    );
    await waitFor(() => {
      expect(screen.getByDisplayValue("Test agent")).toBeInTheDocument();
    });
    expect(
      api.calls.filter((c) => c.path === `/api/agents/${AGENT_ID}`).length,
    ).toBeGreaterThan(1);
  });

  it("blocks the save while a JSON field is not valid", async () => {
    fakeApi();
    renderApp(`/agents/${AGENT_ID}`);
    await userEvent.selectOptions(
      await screen.findByLabelText("Fallback"),
      "fixed",
    );
    const value = screen.getByLabelText("Fixed fallback value (JSON)");
    await userEvent.clear(value);
    await userEvent.type(value, "{{");
    expect(await screen.findByText(/Not valid JSON/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("creates a new agent from the defaults", async () => {
    const created = makeAgent({ version: 1 });
    const api = fakeApi((call) =>
      call.method === "POST" && call.path === "/api/agents"
        ? { status: 201, body: created }
        : undefined,
    );
    const { router } = renderApp("/agents/new");
    const name = await screen.findByDisplayValue(DEFAULTS.name);
    await userEvent.clear(name);
    await userEvent.type(name, "Heating");
    await userEvent.click(screen.getByRole("button", { name: "Create agent" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/agents/${AGENT_ID}`);
    });
    const post = api.calls.find((c) => c.method === "POST");
    expect(
      (post?.body as { definition: AgentDefinition }).definition.name,
    ).toBe("Heating");
  });
});
