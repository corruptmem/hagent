import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  createColumnHelper,
  tableFeatures,
  useTable,
} from "@tanstack/react-table";
import { useMemo, useState } from "react";
import { api } from "../api/client";
import {
  agentQuery,
  diffQuery,
  keys,
  versionQuery,
  versionsQuery,
} from "../api/queries";
import type { VersionSummary } from "../api/types";
import { ErrorMessage } from "../components/ErrorMessage";

const features = tableFeatures({});
const column = createColumnHelper<typeof features, VersionSummary>();

function json(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

/** The versions of an agent: list, view, compare and restore. */
export function Versions({ agentId }: { agentId: string }) {
  const client = useQueryClient();
  const agent = useQuery(agentQuery(agentId));
  const versions = useQuery(versionsQuery(agentId));
  const [from, setFrom] = useState<number | null>(null);
  const [to, setTo] = useState<number | null>(null);
  const [viewing, setViewing] = useState<number | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);

  const list = versions.data ?? [];
  const latest = list[0]?.version ?? 0;
  const fromVersion = from ?? list[1]?.version ?? latest;
  const toVersion = to ?? latest;

  const diff = useQuery({
    ...diffQuery(agentId, fromVersion, toVersion),
    enabled: list.length > 1 && fromVersion !== toVersion,
  });
  const viewed = useQuery({
    ...versionQuery(agentId, viewing ?? 0),
    enabled: viewing !== null,
  });
  const {
    mutate: restoreVersion,
    reset: resetRestore,
    ...restore
  } = useMutation({
    mutationFn: (version: number) =>
      api.restore(agentId, version, agent.data?.version ?? latest),
    onSuccess: async (result) => {
      setRestoring(null);
      client.setQueryData(keys.agent(agentId), result);
      await client.invalidateQueries({ queryKey: keys.agents });
    },
  });

  // Stable columns keep the cell elements mounted between renders.
  const columns = useMemo(
    () =>
      column.columns([
        column.accessor("version", { header: "Version" }),
        column.accessor("change", {
          header: "Change",
          cell: (info) => {
            const v = info.row.original;
            if (v.change === "restore")
              return `restore of version ${v.source_version ?? "?"}`;
            if (v.change === "copy")
              return `copy of ${v.source_agent_id ?? "?"} v${v.source_version ?? "?"}`;
            return v.change;
          },
        }),
        column.accessor("created_at", {
          header: "Time",
          cell: (info) => new Date(info.getValue()).toLocaleString(),
        }),
        column.display({
          id: "compare",
          header: "Compare",
          cell: (info) => {
            const v = info.row.original.version;
            return (
              <span className="actions">
                <label>
                  <input
                    type="radio"
                    name="from"
                    aria-label={`From version ${v}`}
                    checked={fromVersion === v}
                    onChange={() => {
                      setFrom(v);
                    }}
                  />
                  from
                </label>
                <label>
                  <input
                    type="radio"
                    name="to"
                    aria-label={`To version ${v}`}
                    checked={toVersion === v}
                    onChange={() => {
                      setTo(v);
                    }}
                  />
                  to
                </label>
              </span>
            );
          },
        }),
        column.display({
          id: "actions",
          header: "",
          cell: (info) => {
            const v = info.row.original.version;
            return (
              <span className="actions">
                <button
                  type="button"
                  onClick={() => {
                    setViewing(viewing === v ? null : v);
                  }}
                >
                  {viewing === v ? "Hide" : "View"}
                </button>
                {v !== latest ? (
                  <button
                    type="button"
                    onClick={() => {
                      resetRestore();
                      setRestoring(v);
                    }}
                  >
                    Restore
                  </button>
                ) : (
                  <span className="muted">current</span>
                )}
              </span>
            );
          },
        }),
      ]),
    [fromVersion, toVersion, viewing, latest, resetRestore],
  );
  const table = useTable({ features, columns, data: list });

  if (versions.error) return <ErrorMessage error={versions.error} />;
  if (versions.isPending) return <p className="muted">Loading…</p>;

  return (
    <section className="versions">
      {restoring !== null ? (
        <div className="confirm" role="dialog" aria-label="Restore version">
          <p>
            Restore version {restoring}? The app adds a new version with its
            definition. The later versions stay.
          </p>
          <ErrorMessage error={restore.error} />
          <button
            type="button"
            className="primary"
            disabled={restore.isPending}
            onClick={() => {
              restoreVersion(restoring);
            }}
          >
            Restore version {restoring}
          </button>
          <button
            type="button"
            onClick={() => {
              setRestoring(null);
            }}
          >
            Cancel
          </button>
        </div>
      ) : null}
      <table className="data">
        <thead>
          {table.getHeaderGroups().map((group) => (
            <tr key={group.id}>
              {group.headers.map((header) => (
                <th key={header.id}>
                  <table.FlexRender header={header} />
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

      {viewing !== null ? (
        <div className="panel">
          <h3>Version {viewing}</h3>
          <ErrorMessage error={viewed.error} />
          {viewed.data ? (
            <pre aria-label={`Definition of version ${viewing}`}>
              {json(viewed.data.definition)}
            </pre>
          ) : null}
        </div>
      ) : null}

      <div className="panel">
        <h3>
          Changes from version {fromVersion} to version {toVersion}
        </h3>
        {list.length < 2 ? (
          <p className="muted">There is only one version.</p>
        ) : fromVersion === toVersion ? (
          <p className="muted">Select two different versions.</p>
        ) : diff.error ? (
          <ErrorMessage error={diff.error} />
        ) : !diff.data ? (
          <p className="muted">Loading…</p>
        ) : diff.data.changes.length === 0 ? (
          <p className="muted">The definitions are equal.</p>
        ) : (
          <table className="data diff" aria-label="Changes">
            <thead>
              <tr>
                <th>Field</th>
                <th>Version {diff.data.from}</th>
                <th>Version {diff.data.to}</th>
              </tr>
            </thead>
            <tbody>
              {diff.data.changes.map((change) => (
                <tr key={change.path}>
                  <td>
                    <code>{change.path}</code>
                  </td>
                  <td>
                    <pre className="old">{json(change.old)}</pre>
                  </td>
                  <td>
                    <pre className="new">{json(change.new)}</pre>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
