import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { permissionsQuery } from "../../api/queries";
import type { ScopeView } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { AgentSelect, Json } from "./common";
import { when } from "./format";

function Scope({
  title,
  scope,
}: {
  title: string;
  scope: ScopeView | undefined;
}) {
  if (!scope) return null;
  return (
    <div className="panel">
      <h3>{title}</h3>
      {scope.all ? (
        <p>Every entity (admin).</p>
      ) : (
        <>
          {scope.domains.length > 0 ? (
            <p className="muted">Domains: {scope.domains.join(", ")}</p>
          ) : null}
          {scope.entities.length === 0 ? (
            <p className="muted">No entities.</p>
          ) : (
            <ul className="chips" aria-label={`${title} entities`}>
              {scope.entities.map((e) => (
                <li key={e} className="chip">
                  {e}
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

/** The effective scopes of one agent, resolved to entity IDs, and its denied calls. */
export function Permissions() {
  const [agentId, setAgentId] = useState("");
  const view = useQuery({ ...permissionsQuery(agentId), enabled: !!agentId });
  const data = view.data;
  return (
    <section>
      <div className="toolbar">
        <AgentSelect value={agentId} onChange={setAgentId} />
      </div>
      <ErrorMessage error={view.error} />
      {!agentId ? (
        <p className="muted">Select an agent to see its permissions.</p>
      ) : !data ? null : (
        <>
          <p>
            Definition version {data.version}. HA level:{" "}
            <strong>{data.ha.level}</strong>.
          </p>
          {data.registry_stale ? (
            <p className="warning-box">
              The HA registries are not current. The list uses the last known
              registries.
            </p>
          ) : null}
          {data.ha.unavailable ? (
            <p className="warning-box">{data.ha.unavailable}</p>
          ) : null}
          <Scope title="Read scope" scope={data.ha.read} />
          <Scope title="Control scope" scope={data.ha.control} />
          {data.ha.services.length > 0 ? (
            <p>Extra services: {data.ha.services.join(", ")}</p>
          ) : null}
          <div className="panel">
            <h3>Tools</h3>
            <ul className="chips" aria-label="Tools">
              {data.tools.map((t) => (
                <li key={t.name} className="chip">
                  {t.name}
                </li>
              ))}
            </ul>
            <p className="muted">
              Knowledge stores:{" "}
              {data.knowledge.length === 0
                ? "none"
                : data.knowledge
                    .map((k) => `${k.store_id.slice(0, 8)} (${k.access})`)
                    .join(", ")}
            </p>
            <p className="muted">
              Approval needed for: tools{" "}
              {data.approval.tools.join(", ") || "none"}; services{" "}
              {data.approval.services.join(", ") || "none"}; entities{" "}
              {data.approval.entities.join(", ") || "none"}.
            </p>
          </div>
          <h3>Denied calls</h3>
          {data.denied.length === 0 ? (
            <p className="muted">No denied calls.</p>
          ) : (
            <table className="data" aria-label="Denied calls">
              <thead>
                <tr>
                  <th>Time</th>
                  <th>Tool</th>
                  <th>Reason</th>
                  <th>Request</th>
                </tr>
              </thead>
              <tbody>
                {data.denied.map((d) => (
                  <tr key={d.id}>
                    <td>{when(d.created_at)}</td>
                    <td>{d.tool}</td>
                    <td>
                      {d.reason_code}: {d.reason}
                    </td>
                    <td>
                      <Json value={d.request} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  );
}
