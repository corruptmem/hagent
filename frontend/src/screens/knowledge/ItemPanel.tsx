import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";
import { ApiError, api } from "../../api/client";
import { historyQuery, itemQuery, keys } from "../../api/queries";
import type { ItemContent, ItemVersion } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { formatBytes, formatTime } from "../../components/format";

interface PanelProps {
  storeId: string;
  path: string;
  onClose: () => void;
}

/** One item: the editor or the text, the delete action and the history. */
export function ItemPanel({ storeId, path, onClose }: PanelProps) {
  const [section, setSection] = useState("");
  const item = useQuery(itemQuery(storeId, path, section));
  const missing = item.error instanceof ApiError && item.error.status === 404;

  return (
    <section className="panel item-panel" aria-label={`Item ${path}`}>
      <header className="toolbar">
        <h2>{path}</h2>
        <button type="button" onClick={onClose}>
          Close
        </button>
      </header>
      {missing ? (
        <p className="muted">
          This path has no item now. If it was a file, restore a version from
          the history below.
        </p>
      ) : (
        <ErrorMessage error={item.error} />
      )}
      {item.data ? (
        <ItemBody
          key={`${item.data.version}-${section}`}
          storeId={storeId}
          item={item.data}
          onSection={setSection}
          onReload={() => void item.refetch()}
          onDeleted={onClose}
        />
      ) : item.isPending ? (
        <p className="muted">Loading…</p>
      ) : null}
      <History
        storeId={storeId}
        path={path}
        currentVersion={item.data?.version}
      />
    </section>
  );
}

function ItemBody({
  storeId,
  item,
  onSection,
  onReload,
  onDeleted,
}: {
  storeId: string;
  item: ItemContent;
  onSection: (section: string) => void;
  onReload: () => void;
  onDeleted: () => void;
}) {
  const client = useQueryClient();
  const parts = item.parts ?? 1;
  // An item of several parts arrives one part at a time. A save would
  // replace the whole file with one part, so the editor is read-only.
  const editable = item.kind === "file" && parts === 1;
  const [draft, setDraft] = useState(item.content);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const refresh = () =>
    client.invalidateQueries({ queryKey: keys.store(storeId) });

  const save = useMutation({
    mutationFn: () => api.putFile(storeId, item.path, draft, item.version),
    onSuccess: refresh,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteItem(storeId, item.path, item.version),
    onSuccess: async () => {
      await refresh();
      onDeleted();
    },
  });
  const conflict =
    (save.error instanceof ApiError &&
      save.error.code === "version_conflict") ||
    (remove.error instanceof ApiError &&
      remove.error.code === "version_conflict");

  return (
    <div>
      <p className="muted">
        {item.kind} · version {item.version} · {formatBytes(item.size)} ·
        changed {formatTime(item.updated_at)} by{" "}
        {item.updated_by_agent_id ? "an agent" : "the user"}
        {item.media_type ? ` · ${item.media_type}` : ""}
      </p>
      {parts > 1 ? (
        <div className="toolbar">
          <span className="warning">
            This item has {parts} parts. It shows part {item.part ?? 1}.
            {item.kind === "file" ? " Edit large files through an agent." : ""}
          </span>
          <select
            aria-label="Part"
            value={`part ${item.part ?? 1}`}
            onChange={(e) => {
              onSection(e.target.value);
            }}
          >
            {Array.from({ length: parts }, (_, i) => (
              <option key={i} value={`part ${i + 1}`}>
                Part {i + 1}
              </option>
            ))}
          </select>
        </div>
      ) : null}
      {editable ? (
        <textarea
          aria-label="File content"
          className="content"
          rows={18}
          value={draft}
          onChange={(e) => {
            setDraft(e.target.value);
          }}
        />
      ) : (
        <pre className="content" aria-label="Item text">
          {item.content}
        </pre>
      )}
      {conflict ? (
        <div className="error-box" role="alert">
          Another change saved a newer version. Your change is not saved.{" "}
          <button type="button" onClick={onReload}>
            Load the current version
          </button>
        </div>
      ) : (
        <>
          <ErrorMessage error={save.error} />
          <ErrorMessage error={remove.error} />
        </>
      )}
      <div className="toolbar">
        {editable ? (
          <button
            type="button"
            className="primary"
            disabled={draft === item.content || save.isPending}
            onClick={() => {
              save.mutate();
            }}
          >
            Save file
          </button>
        ) : null}
        {item.kind === "document" ? (
          <ReplaceDocument storeId={storeId} path={item.path} />
        ) : null}
        <button
          type="button"
          className="danger"
          onClick={() => {
            setConfirmDelete(true);
          }}
        >
          Delete
        </button>
      </div>
      {confirmDelete ? (
        <div className="confirm" role="dialog" aria-label="Delete item">
          <p>
            Delete <strong>{item.path}</strong>?{" "}
            {item.kind === "file"
              ? "The history keeps its versions, so you can restore it."
              : "A document cannot be restored. Upload it again if you need it."}
          </p>
          <button
            type="button"
            className="danger"
            onClick={() => {
              remove.mutate();
            }}
          >
            Delete item
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
    </div>
  );
}

function ReplaceDocument({ storeId, path }: { storeId: string; path: string }) {
  const client = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const replace = useMutation({
    mutationFn: (file: File) => api.replaceDocument(storeId, path, file),
    onSuccess: () =>
      client.invalidateQueries({ queryKey: keys.store(storeId) }),
    onSettled: () => {
      if (input.current) input.current.value = "";
    },
  });
  return (
    <span className="upload">
      <label className="button">
        Replace document
        <input
          ref={input}
          type="file"
          aria-label="Replace document"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) replace.mutate(file);
          }}
        />
      </label>
      {replace.isPending ? <span className="muted"> Uploading…</span> : null}
      <ErrorMessage error={replace.error} />
    </span>
  );
}

function History({
  storeId,
  path,
  currentVersion,
}: {
  storeId: string;
  path: string;
  currentVersion: number | undefined;
}) {
  const client = useQueryClient();
  const history = useQuery(historyQuery(storeId, path));
  const [viewing, setViewing] = useState<ItemVersion | null>(null);
  const view = useMutation({
    mutationFn: (version: number) => api.historyVersion(storeId, path, version),
    onSuccess: setViewing,
  });
  const restore = useMutation({
    mutationFn: (version: number) =>
      api.restoreItem(storeId, path, version, currentVersion),
    onSuccess: async () => {
      setViewing(null);
      await client.invalidateQueries({ queryKey: keys.store(storeId) });
    },
  });

  if (history.error) {
    return history.error instanceof ApiError && history.error.status === 404 ? (
      <p className="muted">No history for this path.</p>
    ) : (
      <ErrorMessage error={history.error} />
    );
  }
  if (!history.data) return null;
  return (
    <div>
      <h3>History</h3>
      <ErrorMessage error={view.error ?? restore.error} />
      <table className="data">
        <thead>
          <tr>
            <th>Version</th>
            <th>Change</th>
            <th>Author</th>
            <th>Size</th>
            <th>Time</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {history.data.map((v) => {
            const restorable =
              v.kind === "file" && !v.deleted && v.version !== currentVersion;
            return (
              <tr key={v.version}>
                <td>{v.version}</td>
                <td>{v.change}</td>
                <td>{v.author}</td>
                <td>{formatBytes(v.size)}</td>
                <td>{formatTime(v.created_at)}</td>
                <td className="actions">
                  {v.kind === "file" && !v.deleted ? (
                    <button
                      type="button"
                      onClick={() => {
                        view.mutate(v.version);
                      }}
                    >
                      View {v.version}
                    </button>
                  ) : null}
                  {restorable ? (
                    <button
                      type="button"
                      onClick={() => {
                        restore.mutate(v.version);
                      }}
                    >
                      Restore {v.version}
                    </button>
                  ) : null}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {viewing ? (
        <div className="panel" aria-label={`Version ${viewing.version}`}>
          <p className="muted">Version {viewing.version}</p>
          <pre className="content">{viewing.content ?? ""}</pre>
          <button
            type="button"
            onClick={() => {
              setViewing(null);
            }}
          >
            Close version
          </button>
        </div>
      ) : null}
    </div>
  );
}
