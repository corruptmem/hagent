import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { costQuery } from "../../api/queries";
import type { CostTotals } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { AgentSelect } from "./common";
import { formatUsd, useAgentNames } from "./format";

function Rows({
  label,
  name,
  rows,
}: {
  label: string;
  name: string;
  rows: (CostTotals & { key: string })[];
}) {
  return (
    <table className="data" aria-label={label}>
      <thead>
        <tr>
          <th>{name}</th>
          <th>Cost</th>
          <th>Embeddings</th>
          <th>Calls</th>
          <th>Input tokens</th>
          <th>Cached read</th>
          <th>Cached write</th>
          <th>Output tokens</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.key}>
            <td>{r.key}</td>
            <td>{formatUsd(r.cost_usd)}</td>
            <td>{formatUsd(r.embedding_cost_usd)}</td>
            <td>{r.calls}</td>
            <td>{r.input_tokens}</td>
            <td>{r.cache_read_tokens}</td>
            <td>{r.cache_write_tokens}</td>
            <td>{r.output_tokens}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Cost by agent, by day and by path, with cached tokens and embeddings. */
export function Cost() {
  const [days, setDays] = useState(30);
  const [agentId, setAgentId] = useState("");
  const report = useQuery(costQuery(days, agentId));
  const agentName = useAgentNames();
  const data = report.data;
  return (
    <section>
      <div className="toolbar">
        <AgentSelect allowAll value={agentId} onChange={setAgentId} />
        <label className="field inline">
          <span>Days</span>
          <select
            value={days}
            onChange={(e) => {
              setDays(Number(e.target.value));
            }}
          >
            {[1, 7, 30, 90, 366].map((d) => (
              <option key={d} value={d}>
                {d}
              </option>
            ))}
          </select>
        </label>
      </div>
      <ErrorMessage error={report.error} />
      {data ? (
        <>
          <p>
            Total since {data.since}:{" "}
            <strong>{formatUsd(data.total.cost_usd)}</strong>, of which
            embeddings {formatUsd(data.total.embedding_cost_usd)}.{" "}
            {data.total.cache_read_tokens} cached input tokens of{" "}
            {data.total.input_tokens}.
          </p>
          <h3>By agent</h3>
          <Rows
            label="Cost by agent"
            name="Agent"
            rows={data.by_agent.map((r) => ({
              ...r,
              key: r.agent_id === null ? "No agent" : agentName(r.agent_id),
            }))}
          />
          <h3>By day</h3>
          <Rows
            label="Cost by day"
            name="Day"
            rows={data.by_day.map((r) => ({ ...r, key: r.day }))}
          />
          <h3>By path</h3>
          <Rows
            label="Cost by path"
            name="Path"
            rows={data.by_path.map((r) => ({ ...r, key: r.path }))}
          />
          <h3>By call type</h3>
          <Rows
            label="Cost by call type"
            name="Type"
            rows={data.by_type.map((r) => ({ ...r, key: r.call_type }))}
          />
        </>
      ) : null}
    </section>
  );
}
