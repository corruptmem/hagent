import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper,
  createSortedRowModel,
  rowSortingFeature,
  sortFn_alphanumeric,
  sortFn_basic,
  tableFeatures,
  useTable,
} from "@tanstack/react-table";
import { Link, useNavigate } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import { ApiError, api } from "../api/client";
import { agentsQuery, keys } from "../api/queries";
import type { AgentSummary, DeleteChoice } from "../api/types";
import { ErrorMessage } from "../components/ErrorMessage";

const features = tableFeatures({
  rowSortingFeature,
  sortedRowModel: createSortedRowModel(),
  sortFns: { alphanumeric: sortFn_alphanumeric, basic: sortFn_basic },
});
const column = createColumnHelper<typeof features, AgentSummary>();

function sortMark(sorted: false | "asc" | "desc"): string {
  if (sorted === "asc") return " ▲";
  if (sorted === "desc") return " ▼";
  return "";
}

/** The agents list: create, copy, turn on or off, and delete. */
export function AgentsList() {
  const client = useQueryClient();
  const navigate = useNavigate();
  const agents = useQuery(agentsQuery());
  const [deleting, setDeleting] = useState<AgentSummary | null>(null);
  const [actionError, setActionError] = useState<unknown>(null);

  const refresh = () => client.invalidateQueries({ queryKey: keys.agents });
  const onError = (err: unknown) => {
    setActionError(err);
  };

  const { mutate: toggle } = useMutation({
    mutationFn: (a: AgentSummary) => api.setEnabled(a.id, !a.enabled),
    onSuccess: refresh,
    onError,
  });
  const { mutate: copy } = useMutation({
    mutationFn: (a: AgentSummary) => api.copyAgent(a.id),
    onSuccess: async (agent) => {
      await refresh();
      await navigate({ to: "/agents/$agentId", params: { agentId: agent.id } });
    },
    onError,
  });
  const remove = useMutation({
    mutationFn: ({
      agent,
      choice,
    }: {
      agent: AgentSummary;
      choice: DeleteChoice;
    }) => api.deleteAgent(agent.id, choice),
    onSuccess: async () => {
      setDeleting(null);
      await refresh();
    },
    onError,
  });

  // Stable columns keep the cell elements mounted between renders.
  const columns = useMemo(
    () =>
      column.columns([
        column.accessor("name", {
          header: "Name",
          sortFn: "alphanumeric",
          cell: (info) => (
            <Link
              to="/agents/$agentId"
              params={{ agentId: info.row.original.id }}
            >
              {info.getValue()}
            </Link>
          ),
        }),
        column.accessor("enabled", {
          header: "Enabled",
          sortFn: "basic",
          cell: (info) => (
            <label className="switch">
              <input
                type="checkbox"
                aria-label={`Enabled: ${info.row.original.name}`}
                checked={info.getValue()}
                onChange={() => {
                  toggle(info.row.original);
                }}
              />
            </label>
          ),
        }),
        column.accessor("version", { header: "Version", sortFn: "basic" }),
        column.accessor("updated_at", {
          header: "Changed",
          sortFn: "alphanumeric",
          cell: (info) => new Date(info.getValue()).toLocaleString(),
        }),
        column.display({
          id: "actions",
          header: "",
          cell: (info) => (
            <span className="actions">
              <button
                type="button"
                onClick={() => {
                  copy(info.row.original);
                }}
              >
                Copy
              </button>
              <button
                type="button"
                className="danger"
                onClick={() => {
                  setActionError(null);
                  setDeleting(info.row.original);
                }}
              >
                Delete
              </button>
            </span>
          ),
        }),
      ]),
    [toggle, copy],
  );

  const table = useTable({
    features,
    columns,
    data: agents.data ?? [],
    initialState: { sorting: [{ id: "name", desc: false }] },
  });

  return (
    <section>
      <header className="page-header">
        <h1>Agents</h1>
        <Link to="/agents/new" className="button primary">
          New agent
        </Link>
      </header>
      <ErrorMessage error={agents.error ?? actionError} />
      {deleting ? (
        <div className="confirm" role="dialog" aria-label="Delete agent">
          <p>
            Delete <strong>{deleting.name}</strong>? Its versions stay in the
            history. What happens to its private store
            {deleting.private_store ? ` (${deleting.private_store.name})` : ""}?
          </p>
          {actionError instanceof ApiError &&
          actionError.code === "private_store_in_use" ? (
            <p className="muted">
              Other agents attach the private store. Keep it instead.
            </p>
          ) : null}
          <button
            type="button"
            onClick={() => {
              remove.mutate({ agent: deleting, choice: "keep" });
            }}
          >
            Delete agent, keep store
          </button>
          <button
            type="button"
            className="danger"
            onClick={() => {
              remove.mutate({ agent: deleting, choice: "delete" });
            }}
          >
            Delete agent and store
          </button>
          <button
            type="button"
            onClick={() => {
              setDeleting(null);
            }}
          >
            Cancel
          </button>
        </div>
      ) : null}
      {agents.isPending ? (
        <p className="muted">Loading…</p>
      ) : agents.data?.length === 0 ? (
        <p className="muted">No agents yet.</p>
      ) : (
        <table className="data">
          <thead>
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id}>
                {group.headers.map((header) => (
                  <th key={header.id}>
                    {header.column.getCanSort() ? (
                      <button
                        type="button"
                        className="link"
                        onClick={header.column.getToggleSortingHandler()}
                      >
                        <table.FlexRender header={header} />
                        {sortMark(header.column.getIsSorted())}
                      </button>
                    ) : (
                      <table.FlexRender header={header} />
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => (
              <tr key={row.id}>
                {row.getAllCells().map((cell) => (
                  <td key={cell.id}>
                    <table.FlexRender cell={cell} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}
