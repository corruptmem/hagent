import type {
  AgentDefinition,
  ConcurrencyMode,
  FallbackMode,
  HaLevel,
  ResultType,
  RetrievalMode,
  StoreRef,
  ValidationIssue,
} from "../api/types";
import { getPath, issuesFor, setPath } from "../definition";
import {
  Checkbox,
  Field,
  JsonField,
  ModelField,
  NumberInput,
  Select,
  TextInput,
} from "./fields";
import { KnowledgeEditor } from "./KnowledgeEditor";
import { Picker, type PickerOption } from "./Picker";
import type { PickerOptions } from "./pickerOptions";

const RESULT_TYPES: readonly ResultType[] = ["none", "text", "structured"];
const HA_LEVELS: readonly HaLevel[] = ["none", "read", "control", "admin"];
const RETRIEVAL: readonly RetrievalMode[] = ["off", "assist", "all"];
const CONCURRENCY: readonly ConcurrencyMode[] = [
  "queued",
  "single",
  "restart",
  "parallel",
];
const FALLBACK: readonly FallbackMode[] = [
  "last_result",
  "fixed",
  "no_change",
  "error",
];
const TOOL_GROUPS = ["self", "knowledge", "memory", "ha"] as const;
const TOOL_REFS: PickerOption[] = TOOL_GROUPS.flatMap((g) => [
  { value: g, label: `${g} (group)` },
  { value: `${g}.*`, label: `${g}.* (all tools)` },
]);

interface DefinitionFormProps {
  definition: AgentDefinition;
  onChange: (definition: AgentDefinition) => void;
  issues: ValidationIssue[];
  options: PickerOptions;
  privateStore: StoreRef | null;
  stores?: StoreRef[];
  onJsonValidity: (path: string, valid: boolean) => void;
}

/** The form for every field of an agent definition (design section 7.1). */
export function DefinitionForm({
  definition: d,
  onChange,
  issues,
  options,
  privateStore,
  stores,
  onJsonValidity,
}: DefinitionFormProps) {
  const set = (path: string) => (value: unknown) => {
    onChange(setPath(d, path, value));
  };
  const is = (path: string) => issuesFor(issues, path);
  const list = (path: string) => getPath(d, path) as string[];
  const picker = (
    path: string,
    label: string,
    opts: PickerOption[],
    disabled = false,
  ) => (
    <Field path={path} label="" issues={is(path)}>
      <Picker
        path={path}
        label={label}
        options={opts}
        value={list(path)}
        onChange={set(path)}
        disabled={disabled}
      />
    </Field>
  );
  const scopeOff = d.tools.ha.level === "none" || d.tools.ha.level === "admin";
  const scopeAll = d.tools.ha.scope.all;

  return (
    <div className="definition-form">
      <fieldset>
        <legend>General</legend>
        <Field path="name" label="Name" issues={is("name")}>
          <TextInput path="name" value={d.name} onChange={set("name")} />
        </Field>
        <Field
          path="instructions"
          label="Instructions"
          issues={is("instructions")}
        >
          <TextInput
            path="instructions"
            value={d.instructions}
            onChange={set("instructions")}
            multiline
          />
        </Field>
      </fieldset>

      <fieldset>
        <legend>Models</legend>
        <Field path="models.chat" label="Chat model" issues={is("models.chat")}>
          <ModelField
            path="models.chat"
            value={d.models.chat}
            onChange={set("models.chat")}
          />
        </Field>
        <Field
          path="models.decision"
          label="Decision model"
          issues={is("models.decision")}
        >
          <ModelField
            path="models.decision"
            value={d.models.decision}
            onChange={set("models.decision")}
            optional
          />
        </Field>
        <Field
          path="models.summary"
          label="Summary model"
          issues={is("models.summary")}
          help="When not set, the agent uses the chat model for summaries."
        >
          <ModelField
            path="models.summary"
            value={d.models.summary}
            onChange={set("models.summary")}
            optional
            emptyLabel="Chat model"
          />
        </Field>
      </fieldset>

      <fieldset>
        <legend>Inputs and outputs</legend>
        {picker("inputs.entities", "Input entities", options.entities)}
        {picker("inputs.devices", "Input devices", options.devices)}
        {picker(
          "outputs",
          "Outputs (entities the agent may set)",
          options.entities,
        )}
        <Field
          path="result.type"
          label="Result type"
          issues={is("result.type")}
        >
          <Select
            path="result.type"
            value={d.result.type}
            options={RESULT_TYPES}
            onChange={(type) => {
              // The schema field restarts from the stored value, which is valid JSON.
              onJsonValidity("result.schema", true);
              const schema =
                type === "structured"
                  ? (d.result.schema ?? { type: "object", properties: {} })
                  : null;
              onChange(setPath(d, "result", { type, schema }));
            }}
          />
        </Field>
        <Field
          path="result.schema"
          label="Result schema (JSON)"
          issues={is("result.schema")}
          help='Only for the structured type. The schema needs "type": "object".'
        >
          <JsonField
            key={`schema-${d.result.type}`}
            path="result.schema"
            value={d.result.schema}
            onChange={set("result.schema")}
            onValidity={onJsonValidity}
            disabled={d.result.type !== "structured"}
          />
        </Field>
      </fieldset>

      <fieldset>
        <legend>Tools and scopes</legend>
        <div className="row">
          <Checkbox
            path="tools.self"
            label="Self tools"
            value={d.tools.self}
            onChange={set("tools.self")}
          />
          <Checkbox
            path="tools.knowledge"
            label="Knowledge tools"
            value={d.tools.knowledge}
            onChange={set("tools.knowledge")}
          />
          <Checkbox
            path="tools.memory"
            label="Memory tools"
            value={d.tools.memory}
            onChange={set("tools.memory")}
          />
        </div>
        <Field
          path="tools.ha.level"
          label="HA access level"
          issues={is("tools.ha.level")}
        >
          <Select
            path="tools.ha.level"
            value={d.tools.ha.level}
            options={HA_LEVELS}
            onChange={set("tools.ha.level")}
          />
        </Field>
        <Checkbox
          path="tools.ha.scope.all"
          label="Scope: all of HA"
          value={scopeAll}
          disabled={scopeOff}
          onChange={set("tools.ha.scope.all")}
        />
        {issuesFor(issues, "tools.ha.scope")
          .filter((i) => i.loc.length <= 3)
          .map((i, n) => (
            <small key={n} className="error" role="alert">
              {i.msg}
            </small>
          ))}
        {picker(
          "tools.ha.scope.entities",
          "Scope: entities",
          options.entities,
          scopeOff || scopeAll,
        )}
        {picker(
          "tools.ha.scope.devices",
          "Scope: devices",
          options.devices,
          scopeOff || scopeAll,
        )}
        {picker(
          "tools.ha.scope.areas",
          "Scope: areas",
          options.areas,
          scopeOff || scopeAll,
        )}
        {picker(
          "tools.ha.scope.floors",
          "Scope: floors",
          options.floors,
          scopeOff || scopeAll,
        )}
        {picker(
          "tools.ha.scope.labels",
          "Scope: labels",
          options.labels,
          scopeOff || scopeAll,
        )}
        {picker(
          "tools.ha.scope.domains",
          "Scope: domains",
          options.domains,
          scopeOff || scopeAll,
        )}
        {picker(
          "tools.ha.services",
          "Allowed services (control level only)",
          options.services,
          d.tools.ha.level !== "control",
        )}
      </fieldset>

      <fieldset>
        <legend>Knowledge</legend>
        <Field
          path="knowledge"
          label="Attached stores"
          issues={is("knowledge")}
        >
          <KnowledgeEditor
            path="knowledge"
            value={d.knowledge}
            onChange={set("knowledge")}
            privateStore={privateStore}
            {...(stores ? { stores } : {})}
          />
        </Field>
        <Field
          path="retrieval.mode"
          label="Automatic recall"
          issues={is("retrieval.mode")}
        >
          <Select
            path="retrieval.mode"
            value={d.retrieval.mode}
            options={RETRIEVAL}
            onChange={set("retrieval.mode")}
          />
        </Field>
        <div className="row">
          <Field
            path="retrieval.result_count"
            label="Result count"
            issues={is("retrieval.result_count")}
          >
            <NumberInput
              path="retrieval.result_count"
              value={d.retrieval.result_count}
              min={1}
              max={50}
              onChange={set("retrieval.result_count")}
            />
          </Field>
          <Field
            path="retrieval.token_limit"
            label="Token limit"
            issues={is("retrieval.token_limit")}
          >
            <NumberInput
              path="retrieval.token_limit"
              value={d.retrieval.token_limit}
              min={100}
              max={20000}
              onChange={set("retrieval.token_limit")}
            />
          </Field>
        </div>
      </fieldset>

      <fieldset>
        <legend>Approval</legend>
        {picker("approval.tools", "Tools that need approval", TOOL_REFS)}
        {picker(
          "approval.services",
          "Services that need approval",
          options.services,
        )}
        {picker(
          "approval.entities",
          "Entities that need approval",
          options.entities,
        )}
      </fieldset>

      <fieldset>
        <legend>Assist, budget and limits</legend>
        <Checkbox
          path="assist.enabled"
          label="Expose to Assist"
          value={d.assist.enabled}
          onChange={set("assist.enabled")}
        />
        <div className="row">
          <Field
            path="budget.daily_usd"
            label="Daily budget (USD)"
            issues={is("budget.daily_usd")}
          >
            <NumberInput
              path="budget.daily_usd"
              value={d.budget.daily_usd}
              step={0.01}
              min={0}
              max={1000}
              onChange={set("budget.daily_usd")}
            />
          </Field>
          <Field
            path="budget.max_tool_calls"
            label="Tool calls per turn"
            issues={is("budget.max_tool_calls")}
          >
            <NumberInput
              path="budget.max_tool_calls"
              value={d.budget.max_tool_calls}
              min={1}
              max={500}
              onChange={set("budget.max_tool_calls")}
            />
          </Field>
          <Field
            path="budget.max_tokens"
            label="Tokens per turn"
            issues={is("budget.max_tokens")}
          >
            <NumberInput
              path="budget.max_tokens"
              value={d.budget.max_tokens}
              min={1000}
              onChange={set("budget.max_tokens")}
            />
          </Field>
        </div>
        <div className="row">
          <Field
            path="concurrency.mode"
            label="Concurrency"
            issues={is("concurrency.mode")}
          >
            <Select
              path="concurrency.mode"
              value={d.concurrency.mode}
              options={CONCURRENCY}
              onChange={set("concurrency.mode")}
            />
          </Field>
          <Field
            path="concurrency.queue_size"
            label="Queue size"
            issues={is("concurrency.queue_size")}
          >
            <NumberInput
              path="concurrency.queue_size"
              value={d.concurrency.queue_size}
              min={1}
              max={100}
              onChange={set("concurrency.queue_size")}
            />
          </Field>
        </div>
        <Field
          path="fallback.mode"
          label="Fallback"
          issues={is("fallback.mode")}
        >
          <Select
            path="fallback.mode"
            value={d.fallback.mode}
            options={FALLBACK}
            onChange={(mode) => {
              onJsonValidity("fallback.value", true);
              onChange(
                setPath(d, "fallback", {
                  mode,
                  value: mode === "fixed" ? d.fallback.value : null,
                }),
              );
            }}
          />
        </Field>
        <Field
          path="fallback.value"
          label="Fixed fallback value (JSON)"
          issues={is("fallback.value")}
          help="Only for the fixed mode."
        >
          <JsonField
            key={`fallback-${d.fallback.mode}`}
            path="fallback.value"
            value={d.fallback.value}
            onChange={set("fallback.value")}
            onValidity={onJsonValidity}
            disabled={d.fallback.mode !== "fixed"}
          />
        </Field>
      </fieldset>

      <fieldset>
        <legend>Scheduling and review</legend>
        <div className="row">
          <Field
            path="scheduling.min_interval_seconds"
            label="Minimum interval (s)"
            issues={is("scheduling.min_interval_seconds")}
          >
            <NumberInput
              path="scheduling.min_interval_seconds"
              value={d.scheduling.min_interval_seconds}
              min={1}
              max={86400}
              onChange={set("scheduling.min_interval_seconds")}
            />
          </Field>
          <Field
            path="scheduling.max_schedules"
            label="Maximum schedules"
            issues={is("scheduling.max_schedules")}
          >
            <NumberInput
              path="scheduling.max_schedules"
              value={d.scheduling.max_schedules}
              min={0}
              max={1000}
              onChange={set("scheduling.max_schedules")}
            />
          </Field>
          <Field
            path="scheduling.debounce_seconds"
            label="Debounce (s)"
            issues={is("scheduling.debounce_seconds")}
          >
            <NumberInput
              path="scheduling.debounce_seconds"
              value={d.scheduling.debounce_seconds}
              step={0.1}
              min={0}
              max={3600}
              onChange={set("scheduling.debounce_seconds")}
            />
          </Field>
        </div>
        <Checkbox
          path="review.enabled"
          label="Review invocations"
          value={d.review.enabled}
          onChange={set("review.enabled")}
        />
        <Field
          path="review.cron"
          label="Review schedule (cron)"
          issues={is("review.cron")}
          help="Five fields: minute, hour, day of month, month, day of week."
        >
          <TextInput
            path="review.cron"
            value={d.review.cron}
            onChange={set("review.cron")}
          />
        </Field>
      </fieldset>
    </div>
  );
}
