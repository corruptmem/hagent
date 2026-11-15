import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import type { TestRequest } from "../api/types";
import { ErrorMessage } from "../components/ErrorMessage";

const SAMPLE_STATES = `{
  "sensor.<room>_temperature": {"state": "19.5", "attributes": {}}
}`;

function parseObject(
  text: string,
  label: string,
): Record<string, unknown> | undefined {
  if (!text.trim()) return undefined;
  const value: unknown = JSON.parse(text);
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${label} must be a JSON object.`);
  }
  return value as Record<string, unknown>;
}

/** Whether an error means that the app has no agent runtime yet. */
function runtimeMissing(error: unknown): boolean {
  return (
    error instanceof ApiError &&
    (error.code === "not_found" ||
      error.code === "not_ready" ||
      error.status === 501)
  );
}

/**
 * Run a test invocation of the current version with sample data. The app
 * makes no change to HA (docs/api.md, "Test invocation").
 */
export function TestPanel({ agentId }: { agentId: string }) {
  const [text, setText] = useState("");
  const [data, setData] = useState("");
  const [states, setStates] = useState(SAMPLE_STATES);
  const [inputError, setInputError] = useState<string | null>(null);
  const run = useMutation({
    mutationFn: (body: TestRequest) => api.testInvocation(agentId, body),
  });

  return (
    <form
      className="test-panel"
      onSubmit={(e) => {
        e.preventDefault();
        let body: TestRequest;
        try {
          const input: TestRequest["input"] = {};
          if (text.trim()) input.text = text;
          const parsedData = parseObject(data, "Data");
          if (parsedData) input.data = parsedData;
          body = { input };
          const parsedStates = parseObject(states, "Sample states");
          if (parsedStates)
            body.states = parsedStates as NonNullable<TestRequest["states"]>;
        } catch (err) {
          setInputError(err instanceof Error ? err.message : String(err));
          return;
        }
        setInputError(null);
        run.mutate(body);
      }}
    >
      <p className="muted">
        A test invocation runs one agent turn with sample data. The app records
        service calls but does not send them to HA.
      </p>
      <div className="field">
        <label htmlFor="test-text">Text</label>
        <textarea
          id="test-text"
          rows={3}
          value={text}
          onChange={(e) => {
            setText(e.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="test-data">Data (JSON object)</label>
        <textarea
          id="test-data"
          className="json"
          rows={4}
          value={data}
          onChange={(e) => {
            setData(e.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="test-states">Sample states (JSON object)</label>
        <textarea
          id="test-states"
          className="json"
          rows={6}
          value={states}
          onChange={(e) => {
            setStates(e.target.value);
          }}
        />
      </div>
      <button type="submit" className="primary" disabled={run.isPending}>
        Run test
      </button>
      {inputError ? (
        <p className="error-box" role="alert">
          {inputError}
        </p>
      ) : null}
      {run.error && runtimeMissing(run.error) ? (
        <p className="error-box" role="alert">
          The agent runtime is not available yet, so the app cannot run a test
          invocation.
        </p>
      ) : (
        <ErrorMessage error={run.error} />
      )}
      {run.data ? (
        <div className="panel" aria-label="Test result">
          <h3>Result</h3>
          <pre>{JSON.stringify(run.data.result, null, 2)}</pre>
          <h3>Planned service calls</h3>
          <pre>{JSON.stringify(run.data.planned_calls, null, 2)}</pre>
          {run.data.denied_calls.length ? (
            <>
              <h3>Denied calls</h3>
              <pre>{JSON.stringify(run.data.denied_calls, null, 2)}</pre>
            </>
          ) : null}
          <p className="muted">
            {run.data.tool_calls} tool calls · {run.data.tokens} tokens · USD{" "}
            {run.data.cost_usd.toFixed(4)}
          </p>
        </div>
      ) : null}
    </form>
  );
}
