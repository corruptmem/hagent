import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { AGENT_ID, fakeApi } from "../../test/fakeApi";
import {
  INV_ID,
  RULE_ID,
  makeInvocation,
  makeTrace,
} from "../../test/inspectFixtures";
import { renderApp } from "../../test/render";

describe("invocation trace", () => {
  it("shows inputs, messages, tool calls with scope decisions, compactions, result and cost", async () => {
    fakeApi((call) =>
      call.path === `/api/inspect/invocations/${INV_ID}`
        ? { status: 200, body: makeTrace() }
        : undefined,
    );
    renderApp(`/inspect/invocations/${INV_ID}`);
    expect(await screen.findByLabelText("Request")).toHaveTextContent(
      "test room",
    );
    expect(screen.getByLabelText("Model messages")).toHaveTextContent(
      "model message",
    );
    const calls = screen.getByRole("table", { name: "Tool calls" });
    const rows = within(calls).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("allowed");
    expect(rows[1]).toHaveTextContent("denied");
    expect(rows[1]).toHaveTextContent(
      "lock.front is outside the control scope",
    );
    expect(screen.getByLabelText("Compactions")).toHaveTextContent(
      "tool_result_cut",
    );
    expect(screen.getByLabelText("Result")).toHaveTextContent("light on");
    expect(
      screen.getByRole("heading", { name: "Cost: $0.0021" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/600 cached read/)).toBeInTheDocument();
  });

  it("shows the spec of the rule version that answered", async () => {
    const trace = makeTrace({ turns: [] });
    trace.invocation = {
      ...trace.invocation,
      ...makeInvocation({ path: "rule", rule_id: RULE_ID, rule_version: 3 }),
    };
    const api = fakeApi((call) => {
      if (call.path === `/api/inspect/invocations/${INV_ID}`) {
        return { status: 200, body: trace };
      }
      if (call.path === `/api/agents/${AGENT_ID}/rules/${RULE_ID}/versions/3`) {
        return {
          status: 200,
          body: { rule_id: RULE_ID, version: 3, spec: { name: "warm room" } },
        };
      }
      return undefined;
    });
    renderApp(`/inspect/invocations/${INV_ID}`);
    expect(await screen.findByLabelText("Rule spec")).toHaveTextContent(
      "warm room",
    );
    expect(api.calls.some((c) => c.path.includes("/rules/"))).toBe(true);
  });

  it("shows an unknown invocation as an error", async () => {
    fakeApi((call) =>
      call.path.startsWith("/api/inspect/invocations/")
        ? {
            status: 404,
            body: {
              error: { code: "invocation_not_found", message: "no invocation" },
            },
          }
        : undefined,
    );
    renderApp(`/inspect/invocations/${INV_ID}`);
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "invocation_not_found",
    );
  });
});
