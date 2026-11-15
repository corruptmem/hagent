import type { KnowledgeAttachment, StoreAccess, StoreRef } from "../api/types";

interface KnowledgeEditorProps {
  path: string;
  value: KnowledgeAttachment[];
  onChange: (value: KnowledgeAttachment[]) => void;
  /** The private store of the agent. It must stay attached with read_write. */
  privateStore: StoreRef | null;
  /**
   * The stores that the user can attach. Without a list, the editor asks
   * for a store ID. The knowledge stores API (task-11) supplies the list.
   */
  stores?: StoreRef[];
}

const ACCESS: readonly StoreAccess[] = ["read", "read_write"];

/** The knowledge store attachments of a definition. */
export function KnowledgeEditor({
  path,
  value,
  onChange,
  privateStore,
  stores,
}: KnowledgeEditorProps) {
  const names = new Map((stores ?? []).map((s) => [s.id, s.name]));
  if (privateStore)
    names.set(privateStore.id, `${privateStore.name} (private)`);
  const attached = new Set(value.map((a) => a.store_id));
  const update = (index: number, change: Partial<KnowledgeAttachment>) => {
    onChange(value.map((a, i) => (i === index ? { ...a, ...change } : a)));
  };

  return (
    <div className="knowledge" data-path={path}>
      <table>
        <thead>
          <tr>
            <th>Store</th>
            <th>Access</th>
            <th>Inject main file</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {value.length === 0 ? (
            <tr>
              <td colSpan={4} className="muted">
                No stores. On create, the app attaches the private store.
              </td>
            </tr>
          ) : null}
          {value.map((attachment, index) => {
            const isPrivate = attachment.store_id === privateStore?.id;
            return (
              <tr key={index}>
                <td>
                  {stores || isPrivate ? (
                    <span>
                      {names.get(attachment.store_id) ?? attachment.store_id}
                    </span>
                  ) : (
                    <input
                      type="text"
                      aria-label={`Store ID ${index + 1}`}
                      placeholder="Store ID"
                      value={attachment.store_id}
                      onChange={(e) => {
                        update(index, { store_id: e.target.value.trim() });
                      }}
                    />
                  )}
                </td>
                <td>
                  <select
                    aria-label={`Access ${index + 1}`}
                    value={attachment.access}
                    disabled={isPrivate}
                    onChange={(e) => {
                      update(index, { access: e.target.value as StoreAccess });
                    }}
                  >
                    {ACCESS.map((a) => (
                      <option key={a} value={a}>
                        {a}
                      </option>
                    ))}
                  </select>
                </td>
                <td>
                  <input
                    type="checkbox"
                    aria-label={`Inject main file ${index + 1}`}
                    checked={attachment.inject_main}
                    onChange={(e) => {
                      update(index, { inject_main: e.target.checked });
                    }}
                  />
                </td>
                <td>
                  <button
                    type="button"
                    disabled={isPrivate}
                    onClick={() => {
                      onChange(value.filter((_, i) => i !== index));
                    }}
                  >
                    Remove
                  </button>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {stores ? (
        <select
          aria-label="Attach a store"
          value=""
          onChange={(e) => {
            if (e.target.value) {
              onChange([
                ...value,
                {
                  store_id: e.target.value,
                  access: "read",
                  inject_main: false,
                },
              ]);
            }
          }}
        >
          <option value="">Attach a store…</option>
          {stores
            .filter((s) => !attached.has(s.id))
            .map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
        </select>
      ) : (
        <button
          type="button"
          onClick={() => {
            onChange([
              ...value,
              { store_id: "", access: "read", inject_main: false },
            ]);
          }}
        >
          Attach a store
        </button>
      )}
    </div>
  );
}
