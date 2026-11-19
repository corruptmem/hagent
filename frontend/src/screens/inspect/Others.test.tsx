import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { CostReport, CostTotals } from "../../api/types";
import { AGENT_ID, fakeApi } from "../../test/fakeApi";
import {
  APPROVAL_ID,
  makeApproval,
  makeAudit,
} from "../../test/inspectFixtures";
import { renderApp } from "../../test/render";

function totals(cost: number, change: Partial<CostTotals> = {}): CostTotals {
  return {
    calls: 1,
    cost_usd: cost,
    embedding_cost_usd: 0,
    input_tokens: 100,
    cache_read_tokens: 60,
    cache_write_tokens: 0,
    output_tokens: 10,
    ...change,
  };
}

describe("changes to HA", () => {
  it("shows changes with the previous version of an admin change", async () => {
    const api = fakeApi((call) =>
      call.path.startsWith("/api/inspect/audit")
        ? {
            status: 200,
            body: {
              entries: [
                makeAudit({
                  tool: "ha.admin_change",
                  level: "admin",
                  previous: { name: "old helper name" },
                }),
                makeAudit({ id: 3, previous_note: "not an admin change" }),
              ],
            },
          }
        : undefined,
    );
    renderApp("/inspect/changes");
    const table = await screen.findByRole("table", { name: "Audit log" });
    expect(within(table).getByLabelText("Previous version")).toHaveTextContent(
      "old helper name",
    );
    expect(within(table).getByText("not an admin change")).toBeInTheDocument();
    expect(
      api.calls.some((c) => c.path === "/api/inspect/audit?decision=allowed"),
    ).toBe(true);
    await userEvent.selectOptions(screen.getByLabelText("Show"), "denied");
    await waitFor(() => {
      expect(
        api.calls.some((c) => c.path === "/api/inspect/audit?decision=denied"),
      ).toBe(true);
    });
  });
});

describe("cost", () => {
  it("shows totals by agent, day and path with cached tokens and embeddings", async () => {
    const report: CostReport = {
      since: "2026-10-17",
      total: totals(0.875, {
        embedding_cost_usd: 0.125,
        cache_read_tokens: 200,
        input_tokens: 300,
      }),
      by_agent: [
        { agent_id: null, ...totals(0.125) },
        { agent_id: AGENT_ID, ...totals(0.75) },
      ],
      by_day: [{ day: "2026-11-15", ...totals(0.875) }],
      by_path: [
        { path: "agent_turn", ...totals(0.5) },
        { path: "embedding", ...totals(0.125, { embedding_cost_usd: 0.125 }) },
        { path: "rule", ...totals(0.25) },
      ],
      by_type: [{ call_type: "chat", ...totals(0.5) }],
    };
    const api = fakeApi((call) =>
      call.path.startsWith("/api/inspect/cost")
        ? { status: 200, body: report }
        : undefined,
    );
    renderApp("/inspect/cost");
    expect(await screen.findByText(/^Total since/)).toHaveTextContent("$0.88");
    expect(
      screen.getByText(/200 cached input tokens of 300/),
    ).toBeInTheDocument();
    const byAgent = screen.getByRole("table", { name: "Cost by agent" });
    expect(within(byAgent).getByText("Test agent")).toBeInTheDocument();
    expect(within(byAgent).getByText("No agent")).toBeInTheDocument();
    expect(
      screen.getByRole("table", { name: "Cost by day" }),
    ).toHaveTextContent("2026-11-15");
    const byPath = screen.getByRole("table", { name: "Cost by path" });
    expect(within(byPath).getByText("embedding")).toBeInTheDocument();
    expect(within(byPath).getByText("rule")).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText("Days"), "7");
    await waitFor(() => {
      expect(api.calls.some((c) => c.path === "/api/inspect/cost?days=7")).toBe(
        true,
      );
    });
  });
});

describe("schedules", () => {
  it("shows what is active and when each one last ran", async () => {
    fakeApi((call) =>
      call.path.startsWith("/api/inspect/schedules")
        ? {
            status: 200,
            body: {
              items: [
                {
                  id: "s1",
                  agent_id: AGENT_ID,
                  kind: "schedule",
                  spec: { cron: "0 * * * *" },
                  enabled: true,
                  run_at: null,
                  created_at: "2026-11-15T00:00:00+00:00",
                  updated_at: "2026-11-15T00:00:00+00:00",
                  last_run: {
                    due_at: "2026-11-15T10:00:00+00:00",
                    at: "2026-11-15T10:00:00+00:00",
                    status: "submitted",
                    invocation_id: null,
                  },
                  last_event: null,
                },
                {
                  id: "b1",
                  agent_id: AGENT_ID,
                  kind: "subscription",
                  spec: { entities: ["light.test_lamp"] },
                  enabled: false,
                  run_at: null,
                  created_at: "2026-11-15T00:00:00+00:00",
                  updated_at: "2026-11-15T00:00:00+00:00",
                  last_run: null,
                  last_event: null,
                },
              ],
              reviews: [
                {
                  agent_id: AGENT_ID,
                  at: "2026-11-15T04:00:00+00:00",
                  due_at: "2026-11-15T04:00:00+00:00",
                  status: "submitted",
                  invocation_id: null,
                },
              ],
            },
          }
        : undefined,
    );
    renderApp("/inspect/schedules");
    const table = await screen.findByRole("table", {
      name: "Schedules and subscriptions",
    });
    const rows = within(table).getAllByRole("row").slice(1);
    expect(rows[0]).toHaveTextContent("submitted");
    expect(rows[0]).toHaveTextContent("0 * * * *");
    expect(rows[1]).toHaveTextContent("never");
    expect(rows[1]).toHaveTextContent("no");
    expect(screen.getByLabelText("Reviews")).toHaveTextContent("last review");
  });
});

describe("approvals", () => {
  it("lists pending requests", async () => {
    const api = fakeApi((call) =>
      call.path.startsWith("/api/approvals?")
        ? {
            status: 200,
            body: { approvals: [makeApproval()], can_answer: true },
          }
        : undefined,
    );
    renderApp("/inspect/approvals");
    const table = await screen.findByRole("table", {
      name: "Approval requests",
    });
    expect(within(table).getByText("ha.call_service")).toBeInTheDocument();
    expect(within(table).getAllByRole("link")[0]).toHaveAttribute(
      "href",
      `/inspect/approvals/${APPROVAL_ID}`,
    );
    expect(
      api.calls.some((c) => c.path === "/api/approvals?status=pending"),
    ).toBe(true);
  });

  it("answers a pending request through the API", async () => {
    let request = makeApproval({ can_answer: true, answers: [] });
    const api = fakeApi((call) => {
      if (call.path === `/api/approvals/${APPROVAL_ID}`) {
        return { status: 200, body: request };
      }
      if (call.path === `/api/approvals/${APPROVAL_ID}/answer`) {
        request = makeApproval({
          status: "accepted",
          can_answer: false,
          answers: [
            {
              received_at: "2026-11-15T10:01:00+00:00",
              decision: "accept",
              user_id: "user1",
              via: "api",
              outcome: "applied",
            },
          ],
        });
        return { status: 200, body: request };
      }
      return undefined;
    });
    renderApp(`/inspect/approvals/${APPROVAL_ID}`);
    expect(await screen.findByLabelText("Summary")).toHaveTextContent(
      "asks to run ha.call_service",
    );
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "POST",
        path: `/api/approvals/${APPROVAL_ID}/answer`,
        body: { decision: "accept" },
      });
    });
    expect(
      await screen.findByRole("table", { name: "Answers" }),
    ).toHaveTextContent("applied");
    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
  });

  it("shows the refusal when the user is not an approver", async () => {
    fakeApi((call) => {
      if (call.path === `/api/approvals/${APPROVAL_ID}`) {
        return {
          status: 200,
          body: makeApproval({ can_answer: true, answers: [] }),
        };
      }
      if (call.method === "POST") {
        return {
          status: 403,
          body: {
            error: { code: "not_approver", message: "not an approver" },
          },
        };
      }
      return undefined;
    });
    renderApp(`/inspect/approvals/${APPROVAL_ID}`);
    await userEvent.click(
      await screen.findByRole("button", { name: "Reject" }),
    );
    expect(await screen.findByRole("alert")).toHaveTextContent("not_approver");
  });
});
