import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { ApiError, api } from "../api/client";
import {
  keys,
  knowledgeSettingsQuery,
  knowledgeStatusQuery,
  settingsQuery,
} from "../api/queries";
import {
  PROVIDERS,
  type ApprovalSettings,
  type EmbeddingModel,
  type ModelRef,
  type PriceOverride,
  type Provider,
  type SettingValues,
  type SettingsResponse,
} from "../api/types";
import { ErrorMessage } from "../components/ErrorMessage";

const PROVIDER_NAMES: Record<Provider, string> = {
  openai: "OpenAI",
  openrouter: "OpenRouter",
};

/** The settings area (design section 7.2). */
export function Settings() {
  const settings = useQuery(settingsQuery());
  return (
    <section>
      <header className="page-header">
        <h1>Settings</h1>
      </header>
      <ErrorMessage error={settings.error} />
      {settings.data ? (
        <>
          <ApiKeys data={settings.data} />
          <Embedding />
          <Budgets key={budgetKey(settings.data)} data={settings.data} />
          <Approvals
            key={JSON.stringify(settings.data.settings.approvals)}
            data={settings.data}
          />
          <Prices
            key={JSON.stringify([
              settings.data.settings.price_overrides,
              settings.data.settings.genai_prices_auto_update,
            ])}
            data={settings.data}
          />
          <DecisionModels
            key={JSON.stringify(settings.data.settings.decision_models)}
            data={settings.data}
          />
        </>
      ) : settings.isPending ? (
        <p className="muted">Loading…</p>
      ) : null}
    </section>
  );
}

function budgetKey(data: SettingsResponse): string {
  return `${data.settings.installation_daily_budget_usd}-${data.settings.agent_daily_budget_usd}`;
}

/** Save a settings change and put the response in the query cache. */
function usePatch() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (changes: Partial<SettingValues>) => api.patchSettings(changes),
    onSuccess: (data) => {
      client.setQueryData(keys.settings, data);
    },
  });
}

/** An error, with the field list of `settings_invalid`. */
function SettingsError({ error }: { error: unknown }) {
  if (
    error instanceof ApiError &&
    error.code === "settings_invalid" &&
    Array.isArray(error.details)
  ) {
    const details = error.details as { loc: unknown[]; msg: string }[];
    return (
      <div className="error-box" role="alert">
        <p>The settings are not valid:</p>
        <ul>
          {details.map((d, i) => (
            <li key={i}>
              <code>{d.loc.join(".")}</code>: {d.msg}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  return <ErrorMessage error={error} />;
}

// --------------------------------------------------------------- API keys

const SOURCE_TEXT = {
  settings: "set here",
  environment: "from the environment variable",
  options: "from the app options",
} as const;

function ApiKeys({ data }: { data: SettingsResponse }) {
  return (
    <fieldset>
      <legend>Providers and API keys</legend>
      <p className="help">
        The app shows a key only in masked form. It never sends keys to HA and
        never writes them to the log. A key set here wins over the app options
        and the environment.
      </p>
      {PROVIDERS.map((provider) => (
        <ApiKey key={provider} provider={provider} data={data} />
      ))}
    </fieldset>
  );
}

function ApiKey({
  provider,
  data,
}: {
  provider: Provider;
  data: SettingsResponse;
}) {
  const client = useQueryClient();
  const info = data.keys[provider];
  const [value, setValue] = useState("");
  const onSuccess = (result: SettingsResponse) => {
    client.setQueryData(keys.settings, result);
    setValue("");
  };
  const save = useMutation({
    mutationFn: () => api.setKey(provider, value.trim()),
    onSuccess,
  });
  const remove = useMutation({
    mutationFn: () => api.deleteKey(provider),
    onSuccess,
  });
  const label = PROVIDER_NAMES[provider];
  return (
    <form
      className="field key"
      aria-label={`${label} API key`}
      onSubmit={(e) => {
        e.preventDefault();
        save.mutate();
      }}
    >
      <label htmlFor={`key-${provider}`}>{label}</label>
      <p data-testid={`key-status-${provider}`}>
        {info.set ? (
          <>
            Key {info.hint ? <code>{info.hint}</code> : "(hidden)"}{" "}
            <span className="muted">
              {info.source ? SOURCE_TEXT[info.source] : ""}
            </span>
          </>
        ) : (
          <span className="muted">No key</span>
        )}
      </p>
      <div className="row">
        <input
          id={`key-${provider}`}
          type="password"
          autoComplete="off"
          placeholder={info.set ? "Enter a new key" : "Enter the key"}
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
          }}
        />
        <button
          type="submit"
          className="primary"
          disabled={!value.trim() || save.isPending}
        >
          Save key
        </button>
        {info.source === "settings" ? (
          <button
            type="button"
            onClick={() => {
              remove.mutate();
            }}
          >
            Remove key
          </button>
        ) : null}
      </div>
      <ErrorMessage error={save.error ?? remove.error} />
    </form>
  );
}

// -------------------------------------------------------------- embedding

function Embedding() {
  const client = useQueryClient();
  const current = useQuery(knowledgeSettingsQuery());
  const status = useQuery(knowledgeStatusQuery());
  if (current.error) return <ErrorMessage error={current.error} />;
  if (!current.data) return null;
  return (
    <EmbeddingForm
      key={JSON.stringify(current.data.embedding)}
      current={current.data.embedding}
      status={status.data}
      onSaved={async () => {
        await client.invalidateQueries({ queryKey: keys.knowledge });
      }}
    />
  );
}

function EmbeddingForm({
  current,
  status,
  onSaved,
}: {
  current: EmbeddingModel | null;
  status: { search_mode: string; pending_embeddings: number } | undefined;
  onSaved: () => Promise<void>;
}) {
  const [provider, setProvider] = useState<Provider>(
    current?.provider ?? "openai",
  );
  const [model, setModel] = useState(current?.model ?? "");
  const [dimensions, setDimensions] = useState(
    current?.dimensions ? String(current.dimensions) : "",
  );
  const draft: EmbeddingModel | null = model.trim()
    ? {
        provider,
        model: model.trim(),
        ...(dimensions ? { dimensions: Number(dimensions) } : {}),
      }
    : null;
  const changed =
    draft !== null &&
    (draft.provider !== current?.provider ||
      draft.model !== current.model ||
      (draft.dimensions ?? null) !== (current.dimensions ?? null));
  const save = useMutation({
    mutationFn: (value: EmbeddingModel | null) => api.setEmbedding(value),
    onSuccess: onSaved,
  });

  return (
    <fieldset>
      <legend>Embedding model</legend>
      <p className="help">
        The embedding model turns text into vectors for the search of all
        agents. Without a model, search uses keywords only.
      </p>
      <p>
        Current:{" "}
        {current ? (
          <code>
            {current.provider} · {current.model}
            {current.dimensions ? ` · ${current.dimensions} dimensions` : ""}
          </code>
        ) : (
          <span className="muted">none (keyword search only)</span>
        )}
        {status ? (
          <span className="muted">
            {" "}
            · search: {status.search_mode} · {status.pending_embeddings} texts
            wait for a vector
          </span>
        ) : null}
      </p>
      <div className="row">
        <div className="field">
          <label htmlFor="embedding-provider">Provider</label>
          <select
            id="embedding-provider"
            value={provider}
            onChange={(e) => {
              setProvider(e.target.value as Provider);
            }}
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {PROVIDER_NAMES[p]}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="embedding-model">Model</label>
          <input
            id="embedding-model"
            placeholder="text-embedding-3-small"
            value={model}
            onChange={(e) => {
              setModel(e.target.value);
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="embedding-dimensions">Dimensions</label>
          <input
            id="embedding-dimensions"
            type="number"
            min={1}
            placeholder="Model default"
            value={dimensions}
            onChange={(e) => {
              setDimensions(e.target.value);
            }}
          />
        </div>
      </div>
      {changed ? (
        <div className="warning-box" role="note">
          <p>
            With this model, the app sends text to {PROVIDER_NAMES[provider]}:
            the text of documents and files in all stores, the agent history and
            the names of HA entities. The provider charges for it.
          </p>
          <p>
            A change of model starts a full re-index in the background. Search
            uses keywords for texts that wait for a new vector.
          </p>
        </div>
      ) : null}
      <ErrorMessage error={save.error} />
      <button
        type="button"
        className="primary"
        disabled={!changed || save.isPending}
        onClick={() => {
          save.mutate(draft);
        }}
      >
        Save and re-index
      </button>{" "}
      {current ? (
        <button
          type="button"
          onClick={() => {
            save.mutate(null);
          }}
        >
          Turn off vector search
        </button>
      ) : null}
    </fieldset>
  );
}

// ---------------------------------------------------------------- budgets

function numberText(value: number | null): string {
  return value === null ? "" : String(value);
}

function Budgets({ data }: { data: SettingsResponse }) {
  const patch = usePatch();
  const [installation, setInstallation] = useState(
    numberText(data.settings.installation_daily_budget_usd),
  );
  const [agent, setAgent] = useState(
    numberText(data.settings.agent_daily_budget_usd),
  );
  const toValue = (text: string) => (text.trim() ? Number(text) : null);
  return (
    <fieldset>
      <legend>Budgets</legend>
      <p className="help">
        Daily cost limits in USD. They reset at local midnight. An empty field
        uses the app option value.
      </p>
      <div className="row">
        <div className="field">
          <label htmlFor="budget-installation">
            Installation budget (USD per day)
          </label>
          <input
            id="budget-installation"
            type="number"
            min={0}
            step="0.01"
            placeholder={String(data.defaults.installation_daily_budget_usd)}
            value={installation}
            onChange={(e) => {
              setInstallation(e.target.value);
            }}
          />
        </div>
        <div className="field">
          <label htmlFor="budget-agent">
            Default agent budget (USD per day)
          </label>
          <input
            id="budget-agent"
            type="number"
            min={0}
            step="0.01"
            placeholder={String(data.defaults.agent_daily_budget_usd)}
            value={agent}
            onChange={(e) => {
              setAgent(e.target.value);
            }}
          />
        </div>
      </div>
      <SettingsError error={patch.error} />
      <button
        type="button"
        className="primary"
        disabled={patch.isPending}
        onClick={() => {
          patch.mutate({
            installation_daily_budget_usd: toValue(installation),
            agent_daily_budget_usd: toValue(agent),
          });
        }}
      >
        Save budgets
      </button>
    </fieldset>
  );
}

// -------------------------------------------------------------- approvals

function splitList(text: string): string[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function Approvals({ data }: { data: SettingsResponse }) {
  const patch = usePatch();
  const stored = data.settings.approvals;
  const [approvers, setApprovers] = useState(
    (stored?.approvers ?? []).join("\n"),
  );
  const [timeout, setTimeoutText] = useState(
    String(stored?.timeout_seconds ?? 300),
  );
  const [services, setServices] = useState<Record<string, string>>(
    Object.fromEntries(
      Object.entries(stored?.notify_services ?? {}).map(([k, v]) => [
        k,
        v.map((s) => `notify.${s}`).join(", "),
      ]),
    ),
  );
  const ids = splitList(approvers);
  const value: ApprovalSettings = {
    approvers: ids,
    timeout_seconds: Number(timeout),
    notify_services: Object.fromEntries(
      ids
        .map((id) => [id, splitList(services[id] ?? "")] as const)
        .filter(([, list]) => list.length > 0),
    ),
  };
  return (
    <fieldset>
      <legend>Approvers</legend>
      <p className="help">
        The HA users who can accept or reject tool calls that need approval.
        Enter one HA user ID (32 hexadecimal characters) on each line. Find the
        ID in HA under Settings, People, Users.{" "}
        {stored === null
          ? "No approvers are set here: the app uses the approvals app option."
          : ""}
      </p>
      <div className="field">
        <label htmlFor="approvers">Approver user IDs</label>
        <textarea
          id="approvers"
          rows={3}
          value={approvers}
          onChange={(e) => {
            setApprovers(e.target.value);
          }}
        />
      </div>
      {ids.map((id) => (
        <div className="field" key={id}>
          <label htmlFor={`notify-${id}`}>
            Notify services for <code>{id}</code>
          </label>
          <input
            id={`notify-${id}`}
            placeholder="Empty: the phones of the person of this user"
            value={services[id] ?? ""}
            onChange={(e) => {
              setServices({ ...services, [id]: e.target.value });
            }}
          />
          <span className="help">
            Optional. A list of notify.mobile_app_&lt;device&gt; services. It
            replaces the phones of the person of this user.
          </span>
        </div>
      ))}
      <div className="field">
        <label htmlFor="approval-timeout">Time limit (seconds)</label>
        <input
          id="approval-timeout"
          type="number"
          min={1}
          max={3600}
          value={timeout}
          onChange={(e) => {
            setTimeoutText(e.target.value);
          }}
        />
        <span className="help">
          An agent turn waits up to this time. Then the call counts as rejected.
        </span>
      </div>
      <SettingsError error={patch.error} />
      <button
        type="button"
        className="primary"
        disabled={patch.isPending}
        onClick={() => {
          patch.mutate({ approvals: value });
        }}
      >
        Save approvers
      </button>{" "}
      {stored !== null ? (
        <button
          type="button"
          onClick={() => {
            patch.mutate({ approvals: null });
          }}
        >
          Use the app option
        </button>
      ) : null}
    </fieldset>
  );
}

// ----------------------------------------------------------------- prices

interface PriceRow {
  key: string;
  input: string;
  output: string;
  cache_read: string;
  cache_write: string;
}

function toRows(overrides: Record<string, PriceOverride> | null): PriceRow[] {
  return Object.entries(overrides ?? {}).map(([key, p]) => ({
    key,
    input: String(p.input),
    output: p.output === undefined ? "" : String(p.output),
    cache_read: p.cache_read == null ? "" : String(p.cache_read),
    cache_write: p.cache_write == null ? "" : String(p.cache_write),
  }));
}

function fromRows(rows: PriceRow[]): Record<string, PriceOverride> {
  const result: Record<string, PriceOverride> = {};
  for (const row of rows) {
    if (!row.key.trim()) continue;
    const price: PriceOverride = { input: Number(row.input || "0") };
    if (row.output) price.output = Number(row.output);
    if (row.cache_read) price.cache_read = Number(row.cache_read);
    if (row.cache_write) price.cache_write = Number(row.cache_write);
    result[row.key.trim()] = price;
  }
  return result;
}

const RATE_FIELDS = ["input", "output", "cache_read", "cache_write"] as const;

function Prices({ data }: { data: SettingsResponse }) {
  const patch = usePatch();
  const [rows, setRows] = useState<PriceRow[]>(
    toRows(data.settings.price_overrides),
  );
  const autoUpdate =
    data.settings.genai_prices_auto_update ??
    data.defaults.genai_prices_auto_update ??
    false;
  const update = (index: number, change: Partial<PriceRow>) => {
    setRows(rows.map((r, i) => (i === index ? { ...r, ...change } : r)));
  };
  return (
    <fieldset>
      <legend>Prices</legend>
      <p className="help">
        Prices in USD per million tokens. An override wins over the OpenRouter
        price list and the genai-prices library. The key is{" "}
        <code>&lt;price provider&gt;:&lt;model&gt;</code>, for example{" "}
        <code>openai:gpt-6</code> or <code>openrouter:vendor/model</code>.
      </p>
      <table className="data">
        <thead>
          <tr>
            <th>Key</th>
            <th>Input</th>
            <th>Output</th>
            <th>Cache read</th>
            <th>Cache write</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={6} className="muted">
                No overrides.
              </td>
            </tr>
          ) : null}
          {rows.map((row, index) => (
            <tr key={index}>
              <td>
                <input
                  aria-label={`Price key ${index + 1}`}
                  value={row.key}
                  onChange={(e) => {
                    update(index, { key: e.target.value });
                  }}
                />
              </td>
              {RATE_FIELDS.map((field) => (
                <td key={field}>
                  <input
                    type="number"
                    min={0}
                    step="any"
                    aria-label={`${field} ${index + 1}`}
                    value={row[field]}
                    onChange={(e) => {
                      update(index, { [field]: e.target.value });
                    }}
                  />
                </td>
              ))}
              <td>
                <button
                  type="button"
                  onClick={() => {
                    setRows(rows.filter((_, i) => i !== index));
                  }}
                >
                  Remove
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <button
        type="button"
        onClick={() => {
          setRows([
            ...rows,
            { key: "", input: "", output: "", cache_read: "", cache_write: "" },
          ]);
        }}
      >
        Add override
      </button>{" "}
      <button
        type="button"
        className="primary"
        disabled={patch.isPending}
        onClick={() => {
          const overrides = fromRows(rows);
          patch.mutate({
            price_overrides:
              Object.keys(overrides).length > 0 ? overrides : null,
          });
        }}
      >
        Save prices
      </button>
      <label className="checkbox">
        <input
          type="checkbox"
          checked={autoUpdate}
          disabled={patch.isPending}
          onChange={(e) => {
            patch.mutate({ genai_prices_auto_update: e.target.checked });
          }}
        />
        Update the genai-prices data from GitHub once an hour
      </label>
      <SettingsError error={patch.error} />
    </fieldset>
  );
}

// -------------------------------------------------------- decision models

function DecisionModels({ data }: { data: SettingsResponse }) {
  const patch = usePatch();
  const [rows, setRows] = useState<ModelRef[]>(
    data.settings.decision_models ?? data.defaults.decision_models ?? [],
  );
  return (
    <fieldset>
      <legend>Decision models</legend>
      <p className="help">
        Agents and rules can use these decision models. The app has a built-in
        list. Add models that the providers offer and the list does not hold.
      </p>
      <ul>
        {data.built_in_decision_models.map((m) => (
          <li key={`${m.provider}:${m.model}`}>
            <code>
              {m.provider} · {m.model}
            </code>{" "}
            <span className="muted">built in</span>
          </li>
        ))}
      </ul>
      {rows.map((row, index) => (
        <div className="row" key={index}>
          <select
            aria-label={`Decision provider ${index + 1}`}
            value={row.provider}
            onChange={(e) => {
              setRows(
                rows.map((r, i) =>
                  i === index
                    ? { ...r, provider: e.target.value as Provider }
                    : r,
                ),
              );
            }}
          >
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>
                {PROVIDER_NAMES[p]}
              </option>
            ))}
          </select>
          <input
            aria-label={`Decision model ${index + 1}`}
            value={row.model}
            onChange={(e) => {
              setRows(
                rows.map((r, i) =>
                  i === index ? { ...r, model: e.target.value } : r,
                ),
              );
            }}
          />
          <button
            type="button"
            onClick={() => {
              setRows(rows.filter((_, i) => i !== index));
            }}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        onClick={() => {
          setRows([...rows, { provider: "openrouter", model: "" }]);
        }}
      >
        Add model
      </button>{" "}
      <button
        type="button"
        className="primary"
        disabled={patch.isPending}
        onClick={() => {
          const models = rows
            .map((r) => ({ ...r, model: r.model.trim() }))
            .filter((r) => r.model);
          patch.mutate({ decision_models: models.length ? models : null });
        }}
      >
        Save decision models
      </button>
      <SettingsError error={patch.error} />
    </fieldset>
  );
}
