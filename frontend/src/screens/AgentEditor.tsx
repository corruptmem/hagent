import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import {
  agentQuery,
  catalogQuery,
  defaultsQuery,
  keys,
  storesQuery,
} from "../api/queries";
import type { Agent, AgentDefinition } from "../api/types";
import { DefinitionForm } from "../components/DefinitionForm";
import { pickerOptions } from "../components/pickerOptions";
import { ErrorMessage } from "../components/ErrorMessage";
import { issuePath } from "../definition";

/** The form for a new agent. */
export function NewAgent() {
  const defaults = useQuery(defaultsQuery());
  if (defaults.error) return <ErrorMessage error={defaults.error} />;
  if (!defaults.data) return <p className="muted">Loading…</p>;
  return <Editor initial={defaults.data} agent={null} />;
}

/** The form for an existing agent. */
export function EditAgent({ agentId }: { agentId: string }) {
  const agent = useQuery(agentQuery(agentId));
  const [reset, setReset] = useState(0);
  if (agent.error) return <ErrorMessage error={agent.error} />;
  if (!agent.data) return <p className="muted">Loading…</p>;
  return (
    <Editor
      key={`${agentId}-${reset}`}
      initial={agent.data.definition}
      agent={agent.data}
      onReload={async () => {
        await agent.refetch();
        setReset((n) => n + 1);
      }}
    />
  );
}

interface EditorProps {
  initial: AgentDefinition;
  agent: Agent | null;
  onReload?: () => Promise<void>;
}

function Editor({ initial, agent, onReload }: EditorProps) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const catalog = useQuery(catalogQuery());
  // The store picker. If the list does not load, the knowledge editor
  // falls back to a store ID field.
  const storeList = useQuery(storesQuery());
  const stores = storeList.isError
    ? undefined
    : (storeList.data ?? []).map((s) => ({
        id: s.id,
        name: s.private ? `${s.name} (private)` : s.name,
      }));
  const [draft, setDraft] = useState(initial);
  const [baseVersion] = useState(agent?.version ?? 0);
  const [badJson, setBadJson] = useState<Set<string>>(new Set());
  const dirty = JSON.stringify(draft) !== JSON.stringify(initial);

  const save = useMutation({
    mutationFn: (definition: AgentDefinition) =>
      agent
        ? api.updateAgent(agent.id, definition, baseVersion)
        : api.createAgent(definition),
    onSuccess: async (result) => {
      client.setQueryData(keys.agent(result.id), result);
      await client.invalidateQueries({ queryKey: keys.agents });
      if (agent) {
        await onReload?.();
      } else {
        await navigate({
          to: "/agents/$agentId",
          params: { agentId: result.id },
        });
      }
    },
  });

  const error = save.error instanceof ApiError ? save.error : null;
  const issues = error?.issues ?? [];
  const conflict = error?.code === "version_conflict";

  return (
    <form
      className="editor"
      noValidate
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate(draft);
      }}
    >
      <div className="toolbar">
        <button
          type="submit"
          className="primary"
          disabled={
            save.isPending || badJson.size > 0 || (agent !== null && !dirty)
          }
        >
          {agent ? "Save" : "Create agent"}
        </button>
        {agent ? (
          <span className="muted">
            Version {agent.version}
            {dirty ? " · unsaved changes" : ""}
          </span>
        ) : null}
        {catalog.data?.stale ? (
          <span className="warning">HA data may be out of date.</span>
        ) : null}
        {catalog.error ? (
          <span className="warning">HA data is not available.</span>
        ) : null}
      </div>
      {conflict ? (
        <div className="error-box" role="alert">
          Another change saved a newer version. Your edits are not saved.{" "}
          <button type="button" onClick={() => void onReload?.()}>
            Load the current version
          </button>
        </div>
      ) : issues.length ? (
        <div className="error-box" role="alert">
          <p>The definition is not valid:</p>
          <ul>
            {issues.map((issue, i) => (
              <li key={i}>
                <code>{issuePath(issue) || "(definition)"}</code>: {issue.msg}
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <ErrorMessage error={save.error} />
      )}
      <DefinitionForm
        definition={draft}
        onChange={(next) => {
          setDraft(next);
        }}
        issues={issues}
        options={pickerOptions(catalog.data)}
        privateStore={agent?.private_store ?? null}
        {...(stores ? { stores } : {})}
        onJsonValidity={(path, valid) => {
          setBadJson((current) => {
            const next = new Set(current);
            if (valid) next.delete(path);
            else next.add(path);
            return next;
          });
        }}
      />
    </form>
  );
}
