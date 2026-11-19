import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { api } from "../../api/client";
import { traceQuery } from "../../api/queries";
import type { Trace as TraceData, TraceTurn } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { Json } from "./common";
import { formatUsd, useAgentNames, when } from "./format";

/** The spec of the rule version that answered, from the rule routes. */
function RuleSpec({
  agentId,
  ruleId,
  version,
}: {
  agentId: string;
  ruleId: string;
  version: number;
}) {
  const spec = useQuery({
    queryKey: ["rules", agentId, ruleId, "version", version],
    queryFn: () => api.getRuleVersion(agentId, ruleId, version),
    staleTime: Infinity,
  });
  return (
    <div className="panel">
      <h2>
        Rule{" "}
        <Link to="/inspect/rules/$agentId/$ruleId" params={{ agentId, ruleId }}>
          {ruleId.slice(0, 8)}
        </Link>{" "}
        version {version}
      </h2>
      <ErrorMessage error={spec.error} />
      {spec.data ? <Json label="Rule spec" value={spec.data.spec} /> : null}
    </div>
  );
}

function TurnView({ turn }: { turn: TraceTurn }) {
  return (
    <div className="panel">
      <h2>
        Agent turn {turn.dry_run ? "(dry run)" : ""}: {turn.status}
        {turn.stop_reason ? ` (${turn.stop_reason})` : ""}
      </h2>
      {turn.error ? <p className="error">{turn.error}</p> : null}
      <p className="muted">
        {when(turn.started_at)} · {turn.usage.requests} requests ·{" "}
        {turn.usage.input_tokens} input tokens ({turn.usage.cache_read_tokens}{" "}
        cached) · {turn.usage.output_tokens} output tokens
      </p>
      <details>
        <summary>Input</summary>
        <Json label="Turn input" value={turn.input} />
      </details>
      <details>
        <summary>Prompt</summary>
        <pre className="json">{turn.prompt}</pre>
      </details>
      <details>
        <summary>Model messages</summary>
        <Json label="Model messages" value={turn.messages} />
      </details>
      <h3>Tool calls</h3>
      {turn.tool_calls.length === 0 ? (
        <p className="muted">No tool calls.</p>
      ) : (
        <table className="data" aria-label="Tool calls">
          <thead>
            <tr>
              <th>#</th>
              <th>Tool</th>
              <th>Scope decision</th>
              <th>Result</th>
            </tr>
          </thead>
          <tbody>
            {turn.tool_calls.map((call) => (
              <tr key={call.seq}>
                <td>{call.seq}</td>
                <td>
                  <details>
                    <summary>{call.tool}</summary>
                    <Json label="Arguments" value={call.arguments} />
                  </details>
                </td>
                <td>
                  <span
                    className={
                      call.scope_decision === "denied" ? "badge error" : "badge"
                    }
                  >
                    {call.scope_decision}
                  </span>
                  {call.audit?.reason ? ` ${call.audit.reason}` : ""}
                </td>
                <td>
                  <details>
                    <summary>
                      {call.ok ? "ok" : (call.error_code ?? "error")}
                      {call.cut ? " (cut for the model)" : ""}
                    </summary>
                    <Json label="Tool result" value={call.result} />
                  </details>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h3>Compactions</h3>
      {turn.compactions.length === 0 ? (
        <p className="muted">No compactions.</p>
      ) : (
        <ul aria-label="Compactions">
          {turn.compactions.map((c) => (
            <li key={c.id}>
              {c.kind} at {when(c.created_at)}
              {c.summary ? `: ${c.summary}` : ""}
              <Json value={c.detail} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CostView({ trace }: { trace: TraceData }) {
  return (
    <div className="panel">
      <h2>Cost: {formatUsd(trace.cost.total_usd)}</h2>
      <p className="muted">
        {trace.cost.input_tokens} input tokens ({trace.cost.cache_read_tokens}{" "}
        cached read, {trace.cost.cache_write_tokens} cached write) ·{" "}
        {trace.cost.output_tokens} output tokens
      </p>
      {trace.provider_calls.length > 0 ? (
        <table className="data" aria-label="Provider calls">
          <thead>
            <tr>
              <th>Type</th>
              <th>Model</th>
              <th>Tokens in / cached / out</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {trace.provider_calls.map((c) => (
              <tr key={c.id}>
                <td>{c.call_type}</td>
                <td>
                  {c.provider}/{c.model}
                </td>
                <td>
                  {c.input_tokens} / {c.cache_read_tokens} / {c.output_tokens}
                </td>
                <td>{formatUsd(c.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
    </div>
  );
}

/** The full trace of one invocation (design section 7.2). */
export function Trace({ invocationId }: { invocationId: string }) {
  const trace = useQuery(traceQuery(invocationId));
  const agentName = useAgentNames();
  if (trace.error) return <ErrorMessage error={trace.error} />;
  if (!trace.data) return <p className="muted">Loading…</p>;
  const { invocation: inv } = trace.data;
  return (
    <section>
      <p>
        <Link to="/inspect/invocations">All invocations</Link>
      </p>
      <h2>
        {agentName(inv.agent_id)} · {inv.source} · {inv.path ?? "not run"} ·{" "}
        {inv.status}
      </h2>
      <p className="muted">
        Created {when(inv.created_at)}, finished {when(inv.finished_at)}.
        Definition version {inv.agent_version ?? "–"}.
      </p>
      {inv.error ? <p className="error">{inv.error}</p> : null}
      <div className="panel">
        <h2>Inputs</h2>
        <Json label="Request" value={inv.request} />
      </div>
      {inv.rule_id && inv.rule_version ? (
        <RuleSpec
          agentId={inv.agent_id}
          ruleId={inv.rule_id}
          version={inv.rule_version}
        />
      ) : null}
      {trace.data.turns.map((turn) => (
        <TurnView key={turn.id} turn={turn} />
      ))}
      {trace.data.audit.length > 0 ? (
        <div className="panel">
          <h2>Audit rows</h2>
          <ul aria-label="Audit rows">
            {trace.data.audit.map((a) => (
              <li key={a.id}>
                {a.tool}: {a.decision}, {a.status}
                {a.reason ? ` (${a.reason})` : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {trace.data.trials.length > 0 ? (
        <div className="panel">
          <h2>Trials</h2>
          <ul>
            {trace.data.trials.map((t) => (
              <li key={t.id}>
                Version {t.version}: {t.status} ({formatUsd(t.cost_usd)})
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      {trace.data.approvals.length > 0 ? (
        <div className="panel">
          <h2>Approvals</h2>
          <ul>
            {trace.data.approvals.map((a) => (
              <li key={a.id}>
                <Link
                  to="/inspect/approvals/$approvalId"
                  params={{ approvalId: a.id }}
                >
                  {a.tool}
                </Link>
                : {a.status}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
      <div className="panel">
        <h2>Result</h2>
        <Json label="Result" value={inv.result} />
      </div>
      <CostView trace={trace.data} />
    </section>
  );
}
