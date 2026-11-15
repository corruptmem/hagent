import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { SettingsResponse } from "../api/types";
import { fakeApi, type Call } from "../test/fakeApi";
import { renderApp } from "../test/render";

/** The value, or a test failure when it is missing. */
function must<T>(value: T | null | undefined): T {
  if (value == null) throw new Error("missing value");
  return value;
}

const USER = "c".repeat(32);
// The API never sends a full key. The test types one and checks that the
// screen never shows it.
const TYPED_KEY = "sk-typed-test-key-0000-wxyz";

function makeSettings(
  change: Partial<SettingsResponse> = {},
): SettingsResponse {
  const empty = {
    installation_daily_budget_usd: null,
    agent_daily_budget_usd: null,
    price_overrides: null,
    genai_prices_auto_update: null,
    decision_models: null,
    approvals: null,
  };
  return {
    keys: {
      openai: { set: true, hint: "…abcd", source: "options" },
      openrouter: { set: false, hint: null, source: null },
    },
    settings: empty,
    defaults: {
      ...empty,
      installation_daily_budget_usd: 5,
      agent_daily_budget_usd: 0.5,
      price_overrides: {},
      genai_prices_auto_update: false,
      decision_models: [],
    },
    built_in_decision_models: [{ provider: "openai", model: "gpt-6-luna" }],
    ...change,
  };
}

const ok = (body: unknown) => ({ status: 200, body });

function settingsApi(extra: (call: Call) => unknown = () => undefined) {
  return fakeApi((call) => {
    const special = extra(call);
    if (special) return special as { status: number; body: unknown };
    if (call.method === "GET" && call.path === "/api/settings")
      return ok(makeSettings());
    if (call.method === "GET" && call.path === "/api/knowledge/settings")
      return ok({ embedding: null });
    if (call.method === "GET" && call.path === "/api/knowledge/status")
      return ok({
        search_mode: "keyword",
        fallback_reason: "no embedding model",
        sqlite_vec: true,
        embedding: null,
        pending_embeddings: 0,
        last_error: null,
        entity_catalog: 3,
      });
    return undefined;
  });
}

async function openSettings() {
  renderApp("/settings");
  await screen.findByRole("heading", { name: "Settings" });
  await screen.findByText("Providers and API keys");
}

describe("settings", () => {
  it("shows API keys only masked and stores a new key", async () => {
    const api = settingsApi((call) =>
      call.method === "PUT" && call.path === "/api/settings/keys/openrouter"
        ? ok(
            makeSettings({
              keys: {
                openai: { set: true, hint: "…abcd", source: "options" },
                openrouter: { set: true, hint: "…wxyz", source: "settings" },
              },
            }),
          )
        : undefined,
    );
    await openSettings();
    expect(screen.getByTestId("key-status-openai")).toHaveTextContent(
      "Key …abcd from the app options",
    );
    expect(screen.getByTestId("key-status-openrouter")).toHaveTextContent(
      "No key",
    );
    const input = screen.getByLabelText("OpenRouter");
    expect(input).toHaveAttribute("type", "password");
    await userEvent.type(input, TYPED_KEY);
    await userEvent.click(
      within(
        screen.getByRole("form", { name: "OpenRouter API key" }),
      ).getByRole("button", { name: "Save key" }),
    );
    await waitFor(() => {
      expect(screen.getByTestId("key-status-openrouter")).toHaveTextContent(
        "Key …wxyz set here",
      );
    });
    expect(api.calls).toContainEqual({
      method: "PUT",
      path: "/api/settings/keys/openrouter",
      body: { key: TYPED_KEY },
    });
    // The field is empty again, and the page shows no full key.
    expect(input).toHaveValue("");
    expect(document.body.textContent).not.toContain(TYPED_KEY);
    expect(
      screen.getByRole("button", { name: "Remove key" }),
    ).toBeInTheDocument();
  });

  it("states that text goes to the provider before it changes the embedding model", async () => {
    const api = settingsApi((call) =>
      call.method === "PUT" && call.path === "/api/knowledge/settings"
        ? ok({ embedding: call.body })
        : undefined,
    );
    await openSettings();
    const fieldset = must(
      (await screen.findByText("Embedding model")).closest("fieldset"),
    );
    const panel = within(fieldset);
    expect(panel.queryByRole("note")).toBeNull();
    await userEvent.selectOptions(
      panel.getByLabelText("Provider"),
      "openrouter",
    );
    await userEvent.type(
      panel.getByLabelText("Model"),
      "openai/text-embedding-3-small",
    );
    const note = panel.getByRole("note");
    expect(note).toHaveTextContent("the app sends text to OpenRouter");
    expect(note).toHaveTextContent("starts a full re-index");
    await userEvent.click(
      panel.getByRole("button", { name: "Save and re-index" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PUT",
        path: "/api/knowledge/settings",
        body: {
          embedding: {
            provider: "openrouter",
            model: "openai/text-embedding-3-small",
          },
        },
      });
    });
  });

  it("saves the budgets, with an empty field as the default", async () => {
    const api = settingsApi((call) =>
      call.method === "PATCH" ? ok(makeSettings()) : undefined,
    );
    await openSettings();
    const field = screen.getByLabelText("Installation budget (USD per day)");
    expect(field).toHaveAttribute("placeholder", "5");
    await userEvent.type(field, "2.5");
    await userEvent.click(screen.getByRole("button", { name: "Save budgets" }));
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PATCH",
        path: "/api/settings",
        body: {
          installation_daily_budget_usd: 2.5,
          agent_daily_budget_usd: null,
        },
      });
    });
  });

  it("saves approvers with a notify override and shows field errors", async () => {
    let reply: unknown = ok(makeSettings());
    const api = settingsApi((call) =>
      call.method === "PATCH" ? reply : undefined,
    );
    await openSettings();
    await userEvent.type(screen.getByLabelText("Other user IDs"), USER);
    await userEvent.type(
      screen.getByLabelText(/Notify services for/),
      "notify.mobile_app_test_phone",
    );
    const timeout = screen.getByLabelText("Time limit (seconds)");
    await userEvent.clear(timeout);
    await userEvent.type(timeout, "120");
    await userEvent.click(
      screen.getByRole("button", { name: "Save approvers" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PATCH",
        path: "/api/settings",
        body: {
          approvals: {
            approvers: [USER],
            timeout_seconds: 120,
            notify_services: { [USER]: ["notify.mobile_app_test_phone"] },
          },
        },
      });
    });

    reply = {
      status: 422,
      body: {
        error: {
          code: "settings_invalid",
          message: "the settings are not valid",
          details: [
            {
              loc: ["approvals"],
              msg: "approvers: 'x' is not an HA user ID",
              type: "value_error",
            },
          ],
        },
      },
    };
    await userEvent.click(
      screen.getByRole("button", { name: "Save approvers" }),
    );
    expect(await screen.findByText(/is not an HA user ID/)).toBeInTheDocument();
  });

  it("saves price overrides, the auto-update and decision models", async () => {
    const api = settingsApi((call) =>
      call.method === "PATCH" ? ok(makeSettings()) : undefined,
    );
    await openSettings();
    await userEvent.click(screen.getByRole("button", { name: "Add override" }));
    await userEvent.type(screen.getByLabelText("Price key 1"), "openai:m1");
    await userEvent.type(screen.getByLabelText("input 1"), "3");
    await userEvent.type(screen.getByLabelText("output 1"), "9");
    await userEvent.click(screen.getByRole("button", { name: "Save prices" }));
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PATCH",
        path: "/api/settings",
        body: { price_overrides: { "openai:m1": { input: 3, output: 9 } } },
      });
    });

    await userEvent.click(
      screen.getByLabelText(/Update the genai-prices data/),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PATCH",
        path: "/api/settings",
        body: { genai_prices_auto_update: true },
      });
    });

    expect(
      screen.getByText("gpt-6-luna", { exact: false }),
    ).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add model" }));
    await userEvent.type(
      screen.getByLabelText("Decision model 1"),
      "vendor/new-1",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Save decision models" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PATCH",
        path: "/api/settings",
        body: {
          decision_models: [{ provider: "openrouter", model: "vendor/new-1" }],
        },
      });
    });
  });

  it("picks approvers from the HA users list", async () => {
    const other = "e".repeat(32);
    const api = settingsApi((call) => {
      if (call.method === "GET" && call.path === "/api/ha/users")
        return ok({
          source: "auth",
          reason: null,
          users: [
            { id: USER, name: "User A", username: "a_user", is_admin: true },
            { id: other, name: "User B", username: "b_user", is_admin: false },
          ],
        });
      if (call.method === "PATCH") return ok(makeSettings());
      return undefined;
    });
    await openSettings();
    const list = await screen.findByRole("list", { name: "HA users" });
    await userEvent.click(
      within(list).getByLabelText("User A (a_user, administrator)"),
    );
    expect(
      screen.getByLabelText("Notify services for User A"),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Save approvers" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PATCH",
        path: "/api/settings",
        body: {
          approvals: {
            approvers: [USER],
            timeout_seconds: 300,
            notify_services: {},
          },
        },
      });
    });
  });

  it("says when the HA users cannot be listed", async () => {
    settingsApi((call) =>
      call.path === "/api/ha/users"
        ? ok({ source: null, reason: "HA is not connected", users: [] })
        : undefined,
    );
    await openSettings();
    expect(
      await screen.findByText(
        /cannot list the HA users \(HA is not connected\)/,
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Other user IDs")).toBeInTheDocument();
  });
});
