import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { invocationsQuery } from "../../api/queries";
import type { InvocationFilters } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { AgentSelect } from "./common";
import { formatUsd, useAgentNames, when } from "./format";

const PATHS = ["rule", "agent_turn", "fallback"];
const STATUSES = ["queued", "running", "ok", "fallback", "error", "cancelled"];

/**
 * The invocation list with filters, and the history search (`q`). The
 * change events keep it current (docs/api.md, "invocations" topic).
 */
export function Invocations() {
  const [filters, setFilters] = useState<InvocationFilters>({});
  const [search, setSearch] = useState("");
  const list = useQuery(invocationsQuery(filters));
  const agentName = useAgentNames();
  const set = (name: keyof InvocationFilters, value: string) => {
    setFilters((current) =>
      Object.fromEntries(
        Object.entries({ ...current, [name]: value }).filter(([, v]) => v),
      ),
    );
  };

  return (
    <section>
      <div className="toolbar">
        <AgentSelect
          allowAll
          value={filters.agent_id ?? ""}
          onChange={(id) => {
            set("agent_id", id);
          }}
        />
        <label className="field inline">
          <span>Source</span>
          <input
            value={filters.source ?? ""}
            placeholder="any"
            onChange={(e) => {
              set("source", e.target.value.trim());
            }}
          />
        </label>
        <label className="field inline">
          <span>Path</span>
          <select
            value={filters.path ?? ""}
            onChange={(e) => {
              set("path", e.target.value);
            }}
          >
            <option value="">any</option>
            {PATHS.map((p) => (
              <option key={p}>{p}</option>
            ))}
          </select>
        </label>
        <label className="field inline">
          <span>Result</span>
          <select
            value={filters.status ?? ""}
            onChange={(e) => {
              set("status", e.target.value);
            }}
          >
            <option value="">any</option>
            {STATUSES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
        <form
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            set("q", search.trim());
          }}
        >
          <label className="field inline">
            <span>Search history</span>
            <input
              type="search"
              value={search}
              onChange={(e) => {
                setSearch(e.target.value);
              }}
            />
          </label>
          <button type="submit">Search</button>
        </form>
      </div>
      <ErrorMessage error={list.error} />
      {list.isPending ? (
        <p className="muted">Loading…</p>
      ) : list.data?.length === 0 ? (
        <p className="muted">No invocations.</p>
      ) : (
        <table className="data" aria-label="Invocations">
          <thead>
            <tr>
              <th>Time</th>
              <th>Agent</th>
              <th>Source</th>
              <th>Path</th>
              <th>Result</th>
              <th>Cost</th>
            </tr>
          </thead>
          <tbody>
            {(list.data ?? []).map((inv) => (
              <tr key={inv.id}>
                <td>
                  <Link
                    to="/inspect/invocations/$invocationId"
                    params={{ invocationId: inv.id }}
                  >
                    {when(inv.created_at)}
                  </Link>
                </td>
                <td>{agentName(inv.agent_id)}</td>
                <td>{inv.source}</td>
                <td>{inv.path ?? "–"}</td>
                <td>
                  {inv.status}
                  {inv.error_code ? ` (${inv.error_code})` : ""}
                  {inv.fallback_reason ? ` (${inv.fallback_reason})` : ""}
                </td>
                <td>{formatUsd(inv.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
