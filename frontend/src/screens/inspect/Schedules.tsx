import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { schedulesQuery } from "../../api/queries";
import type { ScheduleItem } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { AgentSelect, Json } from "./common";
import { useAgentNames, when } from "./format";

function lastActivity(item: ScheduleItem): string {
  if (item.kind === "schedule") {
    const run = item.last_run;
    return run ? `${when(run.at)}: ${run.status}` : "never";
  }
  const event = item.last_event;
  return event
    ? `${when(event.received_at)}: ${event.outcome} (${event.entity_id ?? event.event_type})`
    : "never";
}

/** The schedules and subscriptions, and when each one last ran. */
export function Schedules() {
  const [agentId, setAgentId] = useState("");
  const data = useQuery(schedulesQuery(agentId));
  const agentName = useAgentNames();
  return (
    <section>
      <div className="toolbar">
        <AgentSelect allowAll value={agentId} onChange={setAgentId} />
      </div>
      <ErrorMessage error={data.error} />
      {data.isPending ? (
        <p className="muted">Loading…</p>
      ) : data.data?.items.length === 0 ? (
        <p className="muted">No schedules or subscriptions.</p>
      ) : (
        <table className="data" aria-label="Schedules and subscriptions">
          <thead>
            <tr>
              <th>Agent</th>
              <th>Kind</th>
              <th>Active</th>
              <th>Spec</th>
              <th>Last run</th>
            </tr>
          </thead>
          <tbody>
            {(data.data?.items ?? []).map((item) => (
              <tr key={item.id}>
                <td>{agentName(item.agent_id)}</td>
                <td>{item.kind}</td>
                <td>{item.enabled ? "yes" : "no"}</td>
                <td>
                  <Json value={item.spec} />
                </td>
                <td>{lastActivity(item)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data.data && data.data.reviews.length > 0 ? (
        <>
          <h3>Reviews</h3>
          <ul aria-label="Reviews">
            {data.data.reviews.map((r) => (
              <li key={r.agent_id}>
                {agentName(r.agent_id)}: last review {when(r.at)} ({r.status})
              </li>
            ))}
          </ul>
        </>
      ) : null}
    </section>
  );
}
