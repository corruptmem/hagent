import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Call } from "../../test/fakeApi";
import { AGENT_ID, fakeApi } from "../../test/fakeApi";
import { RULE_ID, makeRule } from "../../test/inspectFixtures";
import { renderApp } from "../../test/render";

const BASE = `/api/agents/${AGENT_ID}/rules`;

function rulesApi(extra: (call: Call) => unknown = () => undefined) {
  return fakeApi((call) => {
    const reply = extra(call);
    if (reply) return reply as { status: number; body: unknown };
    switch (call.path) {
      case BASE:
        return {
          status: 200,
          body: {
            rules: [
              {
                id: RULE_ID,
                position: 0,
                state: "active",
                current_version: 2,
                latest_version: 2,
                updated_at: "2026-11-15T10:00:00+00:00",
                spec: { name: "warm room" },
              },
            ],
          },
        };
      case `${BASE}/${RULE_ID}`:
        return { status: 200, body: makeRule() };
      case `${BASE}/${RULE_ID}/backtests`:
        return {
          status: 200,
          body: {
            backtests: [
              {
                id: "f".repeat(32),
                rule_id: RULE_ID,
                version: 2,
                status: "ok",
                started_at: "2026-11-15T09:00:00+00:00",
                finished_at: "2026-11-15T09:00:01+00:00",
                candidates: 5,
                matched: 4,
                compared: 4,
                agreed: 3,
                disagreed: 1,
                declined: 0,
                errors: 0,
                skipped: 0,
                agreement_rate: 0.75,
                cost_usd: 0,
                error: null,
              },
            ],
          },
        };
      case `${BASE}/${RULE_ID}/trials`:
        return { status: 200, body: { trials: [] } };
      default:
        return undefined;
    }
  });
}

describe("rules", () => {
  it("lists the rules of the selected agent", async () => {
    rulesApi();
    renderApp("/inspect/rules");
    await screen.findByRole("option", { name: "Test agent" });
    await userEvent.selectOptions(screen.getByLabelText("Agent"), AGENT_ID);
    const link = await screen.findByRole("link", { name: "warm room" });
    expect(link).toHaveAttribute(
      "href",
      `/inspect/rules/${AGENT_ID}/${RULE_ID}`,
    );
  });

  it("shows versions, statistics, backtests and trial agreement", async () => {
    rulesApi();
    renderApp(`/inspect/rules/${AGENT_ID}/${RULE_ID}`);
    const versions = await screen.findByRole("table", { name: "Versions" });
    const rows = within(versions).getAllByRole("row").slice(1);
    expect(rows[1]).toHaveTextContent("2 (current)");
    expect(rows[1]).toHaveTextContent("75% (2 runs)");
    expect(rows[1]).toHaveTextContent("80% (4/5)");
    expect(rows[0]).toHaveTextContent("90% (1 runs)");
    const backtests = await screen.findByRole("table", { name: "Backtests" });
    expect(within(backtests).getByText("3 / 4")).toBeInTheDocument();
    expect(screen.getByText("No trials.")).toBeInTheDocument();
  });

  it("shows the spec of an earlier version", async () => {
    rulesApi((call) =>
      call.path === `${BASE}/${RULE_ID}/versions/1`
        ? {
            status: 200,
            body: { rule_id: RULE_ID, version: 1, spec: { name: "first try" } },
          }
        : undefined,
    );
    renderApp(`/inspect/rules/${AGENT_ID}/${RULE_ID}`);
    await userEvent.click(
      await screen.findByRole("button", { name: "Spec 1" }),
    );
    expect(await screen.findByLabelText("Spec of version 1")).toHaveTextContent(
      "first try",
    );
  });

  it("turns a rule off, rolls it back and deletes it", async () => {
    const api = rulesApi((call) => {
      if (call.method === "POST" && call.path.endsWith("/state")) {
        return {
          status: 200,
          body: { id: RULE_ID, version: 2, state: "off" },
        };
      }
      if (call.method === "POST" && call.path.endsWith("/rollback")) {
        return { status: 200, body: { id: RULE_ID, version: 1 } };
      }
      if (call.method === "DELETE") {
        return { status: 200, body: { deleted: RULE_ID } };
      }
      return undefined;
    });
    const { router } = renderApp(`/inspect/rules/${AGENT_ID}/${RULE_ID}`);
    await userEvent.click(
      await screen.findByRole("button", { name: "Turn off" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Roll back to 1" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "POST",
        path: `${BASE}/${RULE_ID}/state`,
        body: { state: "off" },
      });
      expect(api.calls).toContainEqual({
        method: "POST",
        path: `${BASE}/${RULE_ID}/rollback`,
        body: { version: 1 },
      });
    });
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(api.calls.some((c) => c.method === "DELETE")).toBe(false);
    const dialog = screen.getByRole("dialog", { name: "Delete rule" });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete rule" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "DELETE",
        path: `${BASE}/${RULE_ID}`,
        body: undefined,
      });
    });
    await waitFor(() => {
      expect(router.state.location.pathname).toBe("/inspect/rules");
    });
  });

  it("offers turn on for an off rule and no actions for a deleted rule", async () => {
    let rule = makeRule({ state: "off" });
    rulesApi((call) =>
      call.path === `${BASE}/${RULE_ID}`
        ? { status: 200, body: rule }
        : undefined,
    );
    const view = renderApp(`/inspect/rules/${AGENT_ID}/${RULE_ID}`);
    expect(
      await screen.findByRole("button", { name: "Turn on" }),
    ).toBeInTheDocument();
    view.unmount();
    rule = makeRule({ deleted: true, state: "off" });
    renderApp(`/inspect/rules/${AGENT_ID}/${RULE_ID}`);
    expect(await screen.findByText("deleted")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).toBeNull();
    expect(screen.queryByRole("button", { name: /Roll back/ })).toBeNull();
  });
});
