import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { AGENT_ID, fakeApi } from "../test/fakeApi";
import { renderApp } from "../test/render";

describe("test invocation", () => {
  it("reports that the runtime is not available", async () => {
    const api = fakeApi();
    renderApp(`/agents/${AGENT_ID}/test`);
    await userEvent.type(await screen.findByLabelText("Text"), "Is it warm?");
    await userEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(
      await screen.findByText(/runtime is not available yet/),
    ).toBeInTheDocument();
    const call = api.calls.find((c) => c.method === "POST");
    expect(call?.path).toBe(`/api/agents/${AGENT_ID}/test`);
    expect(call?.body).toEqual({
      input: { text: "Is it warm?" },
      states: {
        "sensor.<room>_temperature": { state: "19.5", attributes: {} },
      },
    });
  });

  it("shows the result and the planned service calls", async () => {
    fakeApi((call) =>
      call.method === "POST"
        ? {
            status: 200,
            body: {
              result: "Warm enough.",
              planned_calls: [{ domain: "light", service: "turn_on" }],
              denied_calls: [],
              tool_calls: 2,
              tokens: 900,
              cost_usd: 0.001,
            },
          }
        : undefined,
    );
    renderApp(`/agents/${AGENT_ID}/test`);
    await userEvent.click(
      await screen.findByRole("button", { name: "Run test" }),
    );
    await waitFor(() => {
      expect(screen.getByLabelText("Test result")).toHaveTextContent(
        "Warm enough.",
      );
    });
    expect(screen.getByLabelText("Test result")).toHaveTextContent("turn_on");
  });

  it("refuses sample data that is not a JSON object", async () => {
    const api = fakeApi();
    renderApp(`/agents/${AGENT_ID}/test`);
    const states = await screen.findByLabelText("Sample states (JSON object)");
    await userEvent.clear(states);
    await userEvent.type(states, "[[1]");
    await userEvent.click(screen.getByRole("button", { name: "Run test" }));
    expect(
      await screen.findByText(/must be a JSON object/),
    ).toBeInTheDocument();
    expect(api.calls.some((c) => c.method === "POST")).toBe(false);
  });
});
