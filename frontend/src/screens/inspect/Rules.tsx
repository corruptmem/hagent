import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { api } from "../../api/client";
import {
  backtestsQuery,
  keys,
  ruleQuery,
  rulesQuery,
  trialsQuery,
} from "../../api/queries";
import type { RuleDetail, RuleState } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { AgentSelect, Json } from "./common";
import { formatRate, formatUsd, when } from "./format";

function ruleName(spec: Record<string, unknown>, id: string): string {
  return typeof spec.name === "string" ? spec.name : id.slice(0, 8);
}

/** The rules of one agent, in the order that the router tests them. */
export function Rules() {
  const [agentId, setAgentId] = useState("");
  const rules = useQuery({ ...rulesQuery(agentId), enabled: !!agentId });
  return (
    <section>
      <div className="toolbar">
        <AgentSelect value={agentId} onChange={setAgentId} />
      </div>
      <ErrorMessage error={rules.error} />
      {!agentId ? (
        <p className="muted">Select an agent to see its rules.</p>
      ) : rules.isPending ? (
        <p className="muted">Loading…</p>
      ) : rules.data?.length === 0 ? (
        <p className="muted">The agent has no rules.</p>
      ) : (
        <table className="data" aria-label="Rules">
          <thead>
            <tr>
              <th>#</th>
              <th>Rule</th>
              <th>State</th>
              <th>Version</th>
              <th>Changed</th>
            </tr>
          </thead>
          <tbody>
            {(rules.data ?? []).map((r) => (
              <tr key={r.id}>
                <td>{r.position + 1}</td>
                <td>
                  <Link
                    to="/inspect/rules/$agentId/$ruleId"
                    params={{ agentId, ruleId: r.id }}
                  >
                    {ruleName(r.spec, r.id)}
                  </Link>
                </td>
                <td>{r.state}</td>
                <td>
                  {r.current_version}
                  {r.latest_version !== r.current_version
                    ? ` (latest ${String(r.latest_version)})`
                    : ""}
                </td>
                <td>{when(r.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/** The spec of one rule version, loaded through the rule routes. */
function VersionSpec({
  agentId,
  ruleId,
  version,
}: {
  agentId: string;
  ruleId: string;
  version: number;
}) {
  const spec = useQuery({
    queryKey: [...keys.rule(agentId, ruleId), "version", version],
    queryFn: () => api.getRuleVersion(agentId, ruleId, version),
    staleTime: Infinity,
  });
  if (spec.error) return <ErrorMessage error={spec.error} />;
  return spec.data ? (
    <Json label={`Spec of version ${String(version)}`} value={spec.data.spec} />
  ) : (
    <p className="muted">Loading…</p>
  );
}

function Versions({
  agentId,
  rule,
  onRollback,
  busy,
}: {
  agentId: string;
  rule: RuleDetail;
  onRollback: (version: number) => void;
  busy: boolean;
}) {
  const [shown, setShown] = useState<number | null>(null);
  return (
    <>
      <table className="data" aria-label="Versions">
        <thead>
          <tr>
            <th>Version</th>
            <th>Matched</th>
            <th>Answered</th>
            <th>Declined</th>
            <th>Cache hits</th>
            <th>Errors</th>
            <th>Cost</th>
            <th>Backtest agreement</th>
            <th>Trial agreement</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rule.versions.map((v) => (
            <tr
              key={v.version}
              className={v.version === rule.current_version ? "selected" : ""}
            >
              <td>
                {v.version}
                {v.version === rule.current_version ? " (current)" : ""}
              </td>
              <td>{v.matched}</td>
              <td>{v.answered}</td>
              <td>{v.declined}</td>
              <td>{v.cache_hits}</td>
              <td title={v.last_error ?? undefined}>{v.errors}</td>
              <td>{formatUsd(v.cost_usd)}</td>
              <td>
                {formatRate(v.backtest?.agreement_rate)} (
                {v.backtest?.runs ?? 0} runs)
              </td>
              <td>
                {formatRate(v.trial?.agreement_rate)} ({v.trial?.agreed ?? 0}/
                {(v.trial?.agreed ?? 0) + (v.trial?.disagreed ?? 0)})
              </td>
              <td>
                <button
                  type="button"
                  aria-expanded={shown === v.version}
                  onClick={() => {
                    setShown(shown === v.version ? null : v.version);
                  }}
                >
                  Spec {v.version}
                </button>
                {v.version !== rule.current_version && !rule.deleted ? (
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => {
                      onRollback(v.version);
                    }}
                  >
                    Roll back to {v.version}
                  </button>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {shown === null ? null : (
        <VersionSpec agentId={agentId} ruleId={rule.id} version={shown} />
      )}
    </>
  );
}

/** One rule: spec, versions with statistics, backtests and trials. */
export function RuleView({
  agentId,
  ruleId,
}: {
  agentId: string;
  ruleId: string;
}) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const rule = useQuery(ruleQuery(agentId, ruleId));
  const backtests = useQuery(backtestsQuery(agentId, ruleId));
  const trials = useQuery(trialsQuery(agentId, ruleId));
  const [confirmDelete, setConfirmDelete] = useState(false);
  const refresh = () => client.invalidateQueries({ queryKey: keys.rules });

  const setState = useMutation({
    mutationFn: (state: RuleState) => api.setRuleState(agentId, ruleId, state),
    onSuccess: refresh,
  });
  const rollback = useMutation({
    mutationFn: (version: number) => api.rollbackRule(agentId, ruleId, version),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteRule(agentId, ruleId),
    onSuccess: async () => {
      await refresh();
      await navigate({ to: "/inspect/rules" });
    },
  });

  if (rule.error) return <ErrorMessage error={rule.error} />;
  if (!rule.data) return <p className="muted">Loading…</p>;
  const data = rule.data;
  const busy = setState.isPending || rollback.isPending || remove.isPending;
  return (
    <section>
      <p>
        <Link to="/inspect/rules">All rules</Link>
      </p>
      <header className="page-header">
        <h2>{ruleName(data.spec, data.id)}</h2>
        <span className="badge">{data.deleted ? "deleted" : data.state}</span>
      </header>
      <ErrorMessage error={setState.error ?? rollback.error ?? remove.error} />
      {data.deleted ? null : (
        <div className="actions">
          {data.state === "off" ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setState.mutate("active");
              }}
            >
              Turn on
            </button>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                setState.mutate("off");
              }}
            >
              Turn off
            </button>
          )}
          <button
            type="button"
            className="danger"
            disabled={busy}
            onClick={() => {
              setConfirmDelete(true);
            }}
          >
            Delete
          </button>
        </div>
      )}
      {confirmDelete ? (
        <div className="confirm" role="dialog" aria-label="Delete rule">
          <p>
            Delete this rule? It stops at once. Its versions and statistics stay
            in the history.
          </p>
          <button
            type="button"
            className="danger"
            onClick={() => {
              remove.mutate();
            }}
          >
            Delete rule
          </button>
          <button
            type="button"
            onClick={() => {
              setConfirmDelete(false);
            }}
          >
            Cancel
          </button>
        </div>
      ) : null}
      <h3>Versions and statistics</h3>
      <Versions
        agentId={agentId}
        rule={data}
        busy={busy}
        onRollback={(v) => {
          rollback.mutate(v);
        }}
      />
      <details>
        <summary>Current spec (templates and context)</summary>
        <Json label="Current spec" value={data.spec} />
      </details>
      <h3>Backtests</h3>
      <ErrorMessage error={backtests.error} />
      {backtests.isPending ? (
        <p className="muted">Loading…</p>
      ) : backtests.data?.length === 0 ? (
        <p className="muted">No backtests.</p>
      ) : (
        <table className="data" aria-label="Backtests">
          <thead>
            <tr>
              <th>Started</th>
              <th>Version</th>
              <th>Status</th>
              <th>Matched</th>
              <th>Agreed / compared</th>
              <th>Agreement</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {(backtests.data ?? []).map((b) => (
              <tr key={b.id}>
                <td>{when(b.started_at)}</td>
                <td>{b.version}</td>
                <td title={b.error ?? undefined}>{b.status}</td>
                <td>{b.matched}</td>
                <td>
                  {b.agreed} / {b.compared}
                </td>
                <td>{formatRate(b.agreement_rate)}</td>
                <td>{formatUsd(b.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <h3>Trials</h3>
      <ErrorMessage error={trials.error} />
      {trials.isPending ? (
        <p className="muted">Loading…</p>
      ) : trials.data?.length === 0 ? (
        <p className="muted">No trials.</p>
      ) : (
        <table className="data" aria-label="Trials">
          <thead>
            <tr>
              <th>Time</th>
              <th>Version</th>
              <th>Status</th>
              <th>Rule result</th>
              <th>Agent result</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {(trials.data ?? []).map((t) => (
              <tr key={t.id}>
                <td>
                  <Link
                    to="/inspect/invocations/$invocationId"
                    params={{ invocationId: t.invocation_id }}
                  >
                    {when(t.created_at)}
                  </Link>
                </td>
                <td>{t.version}</td>
                <td title={t.detail ?? undefined}>{t.status}</td>
                <td>{JSON.stringify(t.rule_result)}</td>
                <td>{JSON.stringify(t.agent_result)}</td>
                <td>{formatUsd(t.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
