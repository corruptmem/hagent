import { useQuery } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { auditQuery } from "../../api/queries";
import { ErrorMessage } from "../../components/ErrorMessage";
import { AgentSelect, Json } from "./common";
import { useAgentNames, when } from "./format";

/** The audit log: each change to HA, with the previous version of admin changes. */
export function Changes() {
  const [agentId, setAgentId] = useState("");
  const [decision, setDecision] = useState("allowed");
  const filters = {
    ...(agentId ? { agent_id: agentId } : {}),
    ...(decision ? { decision } : {}),
  };
  const log = useQuery(auditQuery(filters));
  const agentName = useAgentNames();
  return (
    <section>
      <div className="toolbar">
        <AgentSelect allowAll value={agentId} onChange={setAgentId} />
        <label className="field inline">
          <span>Show</span>
          <select
            value={decision}
            onChange={(e) => {
              setDecision(e.target.value);
            }}
          >
            <option value="allowed">Changes</option>
            <option value="denied">Denied calls</option>
            <option value="">All rows</option>
          </select>
        </label>
      </div>
      <ErrorMessage error={log.error} />
      {log.isPending ? (
        <p className="muted">Loading…</p>
      ) : log.data?.length === 0 ? (
        <p className="muted">No rows.</p>
      ) : (
        <table className="data" aria-label="Audit log">
          <thead>
            <tr>
              <th>Time</th>
              <th>Agent</th>
              <th>Tool</th>
              <th>Status</th>
              <th>Request</th>
              <th>Previous version</th>
            </tr>
          </thead>
          <tbody>
            {(log.data ?? []).map((e) => (
              <tr key={e.id}>
                <td>
                  {e.invocation_id && /^[0-9a-f]{32}$/.test(e.invocation_id) ? (
                    <Link
                      to="/inspect/invocations/$invocationId"
                      params={{ invocationId: e.invocation_id }}
                    >
                      {when(e.created_at)}
                    </Link>
                  ) : (
                    when(e.created_at)
                  )}
                </td>
                <td>{agentName(e.agent_id)}</td>
                <td>
                  {e.tool} ({e.level})
                </td>
                <td>
                  {e.decision === "denied"
                    ? `denied: ${e.reason ?? e.reason_code ?? ""}`
                    : e.status}
                  {e.error ? ` (${e.error})` : ""}
                </td>
                <td>
                  <Json label="Request" value={e.request} />
                </td>
                <td>
                  {e.previous !== null ? (
                    <details>
                      <summary>Previous version</summary>
                      <Json label="Previous version" value={e.previous} />
                    </details>
                  ) : (
                    <span className="muted">{e.previous_note ?? "–"}</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
