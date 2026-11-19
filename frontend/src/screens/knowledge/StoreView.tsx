import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useRef, useState } from "react";
import { ApiError, api } from "../../api/client";
import { itemsQuery, keys, storeQuery } from "../../api/queries";
import type { Item, Store } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { formatBytes, formatTime } from "../../components/format";
import { ItemPanel } from "./ItemPanel";
import { AttachedAgents } from "./KnowledgeList";

/** One store: its settings, its agents, its items and the item panel. */
export function StoreView({ storeId }: { storeId: string }) {
  const store = useQuery(storeQuery(storeId));
  const [selected, setSelected] = useState<string | null>(null);
  const [newFile, setNewFile] = useState(false);

  if (store.error) return <ErrorMessage error={store.error} />;
  if (!store.data) return <p className="muted">Loading…</p>;
  const data = store.data;

  return (
    <section>
      <header className="page-header">
        <h1>{data.name}</h1>
        {data.private ? <span className="badge">private</span> : null}
      </header>
      <StoreSettings key={data.updated_at} store={data} />
      <h2>Attached to</h2>
      <AttachedAgents store={data} />
      <h2>Items</h2>
      <div className="toolbar">
        <UploadDocument storeId={storeId} onUploaded={setSelected} />
        <button
          type="button"
          onClick={() => {
            setNewFile(true);
            setSelected(null);
          }}
        >
          New file
        </button>
        <OpenHistory onOpen={setSelected} />
      </div>
      <Items storeId={storeId} selected={selected} onSelect={setSelected} />
      {newFile ? (
        <NewFile
          storeId={storeId}
          onDone={(path) => {
            setNewFile(false);
            if (path) setSelected(path);
          }}
        />
      ) : null}
      {selected ? (
        <ItemPanel
          key={selected}
          storeId={storeId}
          path={selected}
          onClose={() => {
            setSelected(null);
          }}
        />
      ) : null}
    </section>
  );
}

function StoreSettings({ store }: { store: Store }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState(store.name);
  const [description, setDescription] = useState(store.description);
  const [limit, setLimit] = useState(String(store.size_limit));
  const [confirm, setConfirm] = useState(false);
  const dirty =
    name !== store.name ||
    description !== store.description ||
    limit !== String(store.size_limit);

  const save = useMutation({
    mutationFn: () =>
      api.updateStore(store.id, {
        ...(name !== store.name ? { name: name.trim() } : {}),
        ...(description !== store.description ? { description } : {}),
        ...(limit !== String(store.size_limit)
          ? { size_limit: Number(limit) }
          : {}),
      }),
    onSuccess: async (updated) => {
      client.setQueryData(keys.store(store.id), updated);
      await client.invalidateQueries({ queryKey: keys.stores });
    },
  });
  const remove = useMutation({
    mutationFn: () => api.deleteStore(store.id),
    onSuccess: async () => {
      await client.invalidateQueries({ queryKey: keys.stores });
      await navigate({ to: "/knowledge" });
    },
  });
  const inUse =
    remove.error instanceof ApiError && remove.error.code === "store_in_use";

  return (
    <form
      className="panel"
      aria-label="Store settings"
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <div className="row">
        <div className="field">
          <label htmlFor="edit-store-name">Name</label>
          <input
            id="edit-store-name"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="edit-store-limit">Size limit (bytes)</label>
          <input
            id="edit-store-limit"
            type="number"
            min={1000}
            value={limit}
            onChange={(e) => {
              setLimit(e.target.value);
            }}
          />
          <span className="help">
            In use: {formatBytes(store.size)}. {store.files} files,{" "}
            {store.documents} documents.
          </span>
        </div>
      </div>
      <div className="field">
        <label htmlFor="edit-store-description">Description</label>
        <textarea
          id="edit-store-description"
          rows={2}
          value={description}
          onChange={(e) => {
            setDescription(e.target.value);
          }}
        />
      </div>
      <ErrorMessage error={save.error} />
      <button
        type="submit"
        className="primary"
        disabled={!dirty || save.isPending}
      >
        Save store
      </button>{" "}
      <button
        type="button"
        className="danger"
        onClick={() => {
          remove.reset();
          setConfirm(true);
        }}
      >
        Delete store
      </button>
      {confirm ? (
        <div className="confirm" role="dialog" aria-label="Delete store">
          <p>
            Delete <strong>{store.name}</strong> with all its files, documents
            and their history? This cannot be undone.
          </p>
          {inUse ? (
            <p className="error">
              Agents attach this store. Detach it from each agent first, or
              delete the agent with its private store.
            </p>
          ) : (
            <ErrorMessage error={remove.error} />
          )}
          <button
            type="button"
            className="danger"
            onClick={() => {
              remove.mutate();
            }}
          >
            Delete
          </button>
          <button
            type="button"
            onClick={() => {
              setConfirm(false);
            }}
          >
            Cancel
          </button>
        </div>
      ) : null}
    </form>
  );
}

function Items({
  storeId,
  selected,
  onSelect,
}: {
  storeId: string;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  const items = useQuery(itemsQuery(storeId));
  if (items.error) return <ErrorMessage error={items.error} />;
  if (!items.data) return <p className="muted">Loading…</p>;
  if (items.data.length === 0) return <p className="muted">No items.</p>;
  return (
    <table className="data">
      <thead>
        <tr>
          <th>Path</th>
          <th>Kind</th>
          <th>Version</th>
          <th>Size</th>
          <th>Changed</th>
          <th>By</th>
        </tr>
      </thead>
      <tbody>
        {items.data.map((item: Item) => (
          <tr
            key={item.path}
            className={item.path === selected ? "selected" : undefined}
          >
            <td>
              <button
                type="button"
                className="link"
                onClick={() => {
                  onSelect(item.path);
                }}
              >
                {item.path}
              </button>
            </td>
            <td>
              {item.kind}
              {item.pages ? ` (${item.pages} pages)` : ""}
            </td>
            <td>{item.version}</td>
            <td>{formatBytes(item.size)}</td>
            <td>{formatTime(item.updated_at)}</td>
            <td>{item.updated_by_agent_id ? "agent" : "user"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

const ACCEPT = ".pdf,.md,.markdown,.txt,.text,.html,.htm";

function UploadDocument({
  storeId,
  onUploaded,
}: {
  storeId: string;
  onUploaded: (path: string) => void;
}) {
  const client = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadDocument(storeId, file),
    onSuccess: async (result) => {
      await client.invalidateQueries({ queryKey: keys.store(storeId) });
      onUploaded(result.path);
    },
    onSettled: () => {
      if (input.current) input.current.value = "";
    },
  });
  const exists =
    upload.error instanceof ApiError && upload.error.code === "document_exists";
  return (
    <span className="upload">
      <label className="button">
        Upload document
        <input
          ref={input}
          type="file"
          accept={ACCEPT}
          aria-label="Upload document"
          hidden
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) upload.mutate(file);
          }}
        />
      </label>
      {upload.isPending ? <span className="muted"> Uploading…</span> : null}
      {exists ? (
        <span className="error">
          A document with this name exists. Open it and use Replace.
        </span>
      ) : (
        <ErrorMessage error={upload.error} />
      )}
    </span>
  );
}

function NewFile({
  storeId,
  onDone,
}: {
  storeId: string;
  onDone: (path: string | null) => void;
}) {
  const client = useQueryClient();
  const [path, setPath] = useState("");
  const [content, setContent] = useState("");
  const create = useMutation({
    mutationFn: () => api.putFile(storeId, path.trim(), content),
    onSuccess: async (result) => {
      await client.invalidateQueries({ queryKey: keys.store(storeId) });
      onDone(result.path);
    },
  });
  return (
    <form
      className="panel"
      aria-label="New file"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <div className="field">
        <label htmlFor="new-file-path">Path</label>
        <input
          id="new-file-path"
          placeholder="notes/rooms.md"
          value={path}
          onChange={(e) => {
            setPath(e.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="new-file-content">Content (Markdown)</label>
        <textarea
          id="new-file-content"
          rows={10}
          value={content}
          onChange={(e) => {
            setContent(e.target.value);
          }}
        />
      </div>
      <ErrorMessage error={create.error} />
      <button
        type="submit"
        className="primary"
        disabled={!path.trim() || create.isPending}
      >
        Create file
      </button>{" "}
      <button
        type="button"
        onClick={() => {
          onDone(null);
        }}
      >
        Cancel
      </button>
    </form>
  );
}

/**
 * Open the history of a path by name. The item list does not show deleted
 * files, so this is the way to restore one.
 */
function OpenHistory({ onOpen }: { onOpen: (path: string) => void }) {
  const [path, setPath] = useState("");
  return (
    <form
      className="inline"
      aria-label="Open a path"
      onSubmit={(e) => {
        e.preventDefault();
        if (path.trim()) onOpen(path.trim());
      }}
    >
      <input
        aria-label="Path to open"
        placeholder="Path, also of a deleted file"
        value={path}
        onChange={(e) => {
          setPath(e.target.value);
        }}
      />
      <button type="submit" disabled={!path.trim()}>
        Open
      </button>
    </form>
  );
}
