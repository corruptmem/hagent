import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link, useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { api } from "../../api/client";
import { keys, storesQuery } from "../../api/queries";
import type { Store } from "../../api/types";
import { ErrorMessage } from "../../components/ErrorMessage";
import { formatBytes } from "../../components/format";

/** The agents that attach a store, as links. */
export function AttachedAgents({ store }: { store: Store }) {
  if (store.attached.length === 0)
    return <span className="muted">No agent</span>;
  return (
    <ul className="attached">
      {store.attached.map((a) => (
        <li key={a.agent_id}>
          <Link to="/agents/$agentId" params={{ agentId: a.agent_id }}>
            {a.agent_name}
          </Link>{" "}
          <span className="muted">
            {a.access}
            {a.inject_main ? ", main file" : ""}
          </span>
        </li>
      ))}
    </ul>
  );
}

/** The store list: each store with its size and its agents, and a create form. */
export function KnowledgeList() {
  const stores = useQuery(storesQuery());
  const [creating, setCreating] = useState(false);

  return (
    <section>
      <header className="page-header">
        <h1>Knowledge</h1>
        <button
          type="button"
          className="primary"
          onClick={() => {
            setCreating(true);
          }}
        >
          New store
        </button>
      </header>
      {creating ? (
        <CreateStore
          onClose={() => {
            setCreating(false);
          }}
        />
      ) : null}
      <ErrorMessage error={stores.error} />
      {stores.isPending ? (
        <p className="muted">Loading…</p>
      ) : stores.data?.length === 0 ? (
        <p className="muted">No stores yet.</p>
      ) : (
        <table className="data">
          <thead>
            <tr>
              <th>Name</th>
              <th>Description</th>
              <th>Size</th>
              <th>Files</th>
              <th>Documents</th>
              <th>Attached to</th>
            </tr>
          </thead>
          <tbody>
            {stores.data?.map((store) => (
              <tr key={store.id}>
                <td>
                  <Link to="/knowledge/$storeId" params={{ storeId: store.id }}>
                    {store.name}
                  </Link>
                  {store.private ? (
                    <span className="badge">private</span>
                  ) : null}
                </td>
                <td>{store.description}</td>
                <td>
                  {formatBytes(store.size)} of {formatBytes(store.size_limit)}
                </td>
                <td>{store.files}</td>
                <td>{store.documents}</td>
                <td>
                  <AttachedAgents store={store} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </section>
  );
}

function CreateStore({ onClose }: { onClose: () => void }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [limit, setLimit] = useState("");
  const create = useMutation({
    mutationFn: () =>
      api.createStore({
        name: name.trim(),
        description,
        ...(limit ? { size_limit: Number(limit) } : {}),
      }),
    onSuccess: async (store) => {
      await client.invalidateQueries({ queryKey: keys.stores });
      onClose();
      await navigate({
        to: "/knowledge/$storeId",
        params: { storeId: store.id },
      });
    },
  });
  return (
    <form
      className="panel"
      aria-label="New store"
      onSubmit={(e) => {
        e.preventDefault();
        create.mutate();
      }}
    >
      <div className="field">
        <label htmlFor="store-name">Name</label>
        <input
          id="store-name"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
          }}
        />
        <span className="help">
          1 to 64 letters, digits, _ or -. It starts with a letter or a digit.
        </span>
      </div>
      <div className="field">
        <label htmlFor="store-description">Description</label>
        <textarea
          id="store-description"
          rows={2}
          value={description}
          onChange={(e) => {
            setDescription(e.target.value);
          }}
        />
      </div>
      <div className="field">
        <label htmlFor="store-limit">Size limit (bytes)</label>
        <input
          id="store-limit"
          type="number"
          min={1000}
          placeholder="Default"
          value={limit}
          onChange={(e) => {
            setLimit(e.target.value);
          }}
        />
      </div>
      <ErrorMessage error={create.error} />
      <button
        type="submit"
        className="primary"
        disabled={!name.trim() || create.isPending}
      >
        Create store
      </button>{" "}
      <button type="button" onClick={onClose}>
        Cancel
      </button>
    </form>
  );
}
