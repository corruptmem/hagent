import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "@tanstack/react-router";
import { useState } from "react";
import { api } from "../../api/client";
import { approvalQuery, approvalsQuery, keys } from "../../api/queries";
import { ErrorMessage } from "../../components/ErrorMessage";
import { Json } from "./common";
import { useAgentNames, when } from "./format";

const STATUSES = [
  "pending",
  "accepted",
  "rejected",
  "timeout",
  "failed",
  "cancelled",
  "expired",
];

/** The approval requests, newest first. Pending ones show by default. */
export function Approvals() {
  const [status, setStatus] = useState("pending");
  const list = useQuery(approvalsQuery(status));
  const agentName = useAgentNames();
  return (
    <section>
      <div className="toolbar">
        <label className="field inline">
          <span>Status</span>
          <select
            value={status}
            onChange={(e) => {
              setStatus(e.target.value);
            }}
          >
            <option value="">all</option>
            {STATUSES.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
        </label>
      </div>
      <ErrorMessage error={list.error} />
      {list.data && !list.data.can_answer ? (
        <p className="muted">
          You are not an approver. You can see the requests, but not answer
          them.
        </p>
      ) : null}
      {list.isPending ? (
        <p className="muted">Loading…</p>
      ) : list.data?.approvals.length === 0 ? (
        <p className="muted">No approval requests.</p>
      ) : (
        <table className="data" aria-label="Approval requests">
          <thead>
            <tr>
              <th>Created</th>
              <th>Agent</th>
              <th>Tool</th>
              <th>Status</th>
              <th>Expires</th>
            </tr>
          </thead>
          <tbody>
            {(list.data?.approvals ?? []).map((a) => (
              <tr key={a.id}>
                <td>
                  <Link
                    to="/inspect/approvals/$approvalId"
                    params={{ approvalId: a.id }}
                  >
                    {when(a.created_at)}
                  </Link>
                </td>
                <td>{agentName(a.agent_id)}</td>
                <td>{a.tool}</td>
                <td>{a.status}</td>
                <td>{when(a.expires_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

/**
 * One approval request with its answers. An approver answers through the
 * API. The app takes the HA user from the Ingress header, so the UI only
 * posts the decision.
 */
export function ApprovalView({ approvalId }: { approvalId: string }) {
  const client = useQueryClient();
  const request = useQuery(approvalQuery(approvalId));
  const agentName = useAgentNames();
  const answer = useMutation({
    mutationFn: (decision: "accept" | "reject") =>
      api.answerApproval(approvalId, decision),
    onSettled: () => client.invalidateQueries({ queryKey: keys.approvals }),
  });
  if (request.error) return <ErrorMessage error={request.error} />;
  if (!request.data) return <p className="muted">Loading…</p>;
  const a = request.data;
  return (
    <section>
      <p>
        <Link to="/inspect/approvals">All approval requests</Link>
      </p>
      <h2>
        {agentName(a.agent_id)} asks to run {a.tool}
      </h2>
      <p>
        Status: <strong>{a.status}</strong>
        {a.reason ? ` (${a.reason})` : ""}. Created {when(a.created_at)},
        expires {when(a.expires_at)}.
      </p>
      <pre className="json" aria-label="Summary">
        {a.summary}
      </pre>
      <details>
        <summary>Request</summary>
        <Json value={a.request} />
      </details>
      {a.problems.length > 0 ? (
        <div className="warning-box">
          <p>Notification problems:</p>
          <ul>
            {a.problems.map((p) => (
              <li key={p}>{p}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {a.invocation_id ? (
        <p>
          <Link
            to="/inspect/invocations/$invocationId"
            params={{ invocationId: a.invocation_id }}
          >
            Open the invocation
          </Link>
        </p>
      ) : null}
      <ErrorMessage error={answer.error} />
      {a.can_answer ? (
        <div className="actions">
          <button
            type="button"
            className="primary"
            disabled={answer.isPending}
            onClick={() => {
              answer.mutate("accept");
            }}
          >
            Accept
          </button>
          <button
            type="button"
            className="danger"
            disabled={answer.isPending}
            onClick={() => {
              answer.mutate("reject");
            }}
          >
            Reject
          </button>
        </div>
      ) : null}
      <h3>Answers</h3>
      {!a.answers || a.answers.length === 0 ? (
        <p className="muted">No answers.</p>
      ) : (
        <table className="data" aria-label="Answers">
          <thead>
            <tr>
              <th>Received</th>
              <th>Decision</th>
              <th>Via</th>
              <th>Outcome</th>
            </tr>
          </thead>
          <tbody>
            {a.answers.map((ans) => (
              <tr key={`${ans.received_at}-${ans.via}`}>
                <td>{when(ans.received_at)}</td>
                <td>{ans.decision}</td>
                <td>{ans.via}</td>
                <td>{ans.outcome}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
