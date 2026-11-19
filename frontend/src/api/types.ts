// Types of the management API. docs/api.md is the contract.

export type Provider = "openai" | "openrouter";
export const PROVIDERS: readonly Provider[] = ["openai", "openrouter"];

export interface ModelRef {
  provider: Provider;
  model: string;
}

export interface HaScope {
  all: boolean;
  entities: string[];
  devices: string[];
  areas: string[];
  floors: string[];
  labels: string[];
  domains: string[];
}

export type HaLevel = "none" | "read" | "control" | "admin";
export type StoreAccess = "read" | "read_write";

export interface KnowledgeAttachment {
  store_id: string;
  access: StoreAccess;
  inject_main: boolean;
}

export type ResultType = "none" | "text" | "structured";
export type RetrievalMode = "off" | "assist" | "all";
export type ConcurrencyMode = "queued" | "single" | "restart" | "parallel";
export type FallbackMode = "last_result" | "fixed" | "no_change" | "error";

export interface AgentDefinition {
  name: string;
  instructions: string;
  models: {
    chat: ModelRef;
    decision: ModelRef | null;
    summary: ModelRef | null;
  };
  inputs: { entities: string[]; devices: string[] };
  outputs: string[];
  result: { type: ResultType; schema: Record<string, unknown> | null };
  tools: {
    self: boolean;
    knowledge: boolean;
    memory: boolean;
    ha: { level: HaLevel; scope: HaScope; services: string[] };
  };
  knowledge: KnowledgeAttachment[];
  retrieval: { mode: RetrievalMode; result_count: number; token_limit: number };
  approval: { tools: string[]; services: string[]; entities: string[] };
  assist: { enabled: boolean };
  budget: {
    /** null: the default agent budget of the settings. */
    daily_usd: number | null;
    max_tool_calls: number;
    max_tokens: number;
  };
  concurrency: { mode: ConcurrencyMode; queue_size: number };
  fallback: { mode: FallbackMode; value: unknown };
  scheduling: {
    min_interval_seconds: number;
    max_schedules: number;
    debounce_seconds: number;
  };
  review: { enabled: boolean; cron: string };
}

export interface StoreRef {
  id: string;
  name: string;
}

export interface AgentSummary {
  id: string;
  name: string;
  enabled: boolean;
  version: number;
  created_at: string;
  updated_at: string;
  private_store: StoreRef | null;
}

export interface Agent extends AgentSummary {
  definition: AgentDefinition;
}

export type VersionChange = "create" | "edit" | "copy" | "restore";

export interface VersionSummary {
  version: number;
  change: VersionChange;
  source_agent_id: string | null;
  source_version: number | null;
  created_at: string;
}

export interface Version extends VersionSummary {
  definition: AgentDefinition;
}

export interface FieldChange {
  path: string;
  old: unknown;
  new: unknown;
}

export interface Diff {
  from: number;
  to: number;
  changes: FieldChange[];
}

export interface ValidationIssue {
  loc: (string | number)[];
  msg: string;
  type: string;
}

export interface CatalogEntity {
  entity_id: string;
  name: string;
  domain: string;
  state: string | null;
  device_id: string | null;
  area_id: string | null;
  labels: string[];
  disabled: boolean;
}

export interface Catalog {
  stale: boolean;
  entities: CatalogEntity[];
  devices: {
    id: string;
    name: string;
    area_id: string | null;
    labels: string[];
  }[];
  areas: { id: string; name: string; floor_id: string | null }[];
  floors: { id: string; name: string }[];
  labels: { id: string; name: string }[];
  domains: string[];
  services: string[];
}

export interface TestRequest {
  input: { text?: string; data?: Record<string, unknown> };
  states?: Record<
    string,
    { state: string; attributes: Record<string, unknown> }
  >;
}

export interface PlannedCall {
  domain: string;
  service: string;
  target?: Record<string, unknown>;
  data?: Record<string, unknown>;
  reason?: string;
}

export interface TestResponse {
  result: unknown;
  planned_calls: PlannedCall[];
  denied_calls: PlannedCall[];
  tool_calls: number;
  tokens: number;
  cost_usd: number;
}

export type DeleteChoice = "keep" | "delete";

// Knowledge stores (docs/api.md, "Knowledge stores").

export interface StoreAttachment {
  agent_id: string;
  agent_name: string;
  access: StoreAccess;
  inject_main: boolean;
}

export interface Store {
  id: string;
  name: string;
  description: string;
  private: boolean;
  owner_agent_id: string | null;
  size_limit: number;
  size: number;
  files: number;
  documents: number;
  created_at: string;
  updated_at: string;
  attached: StoreAttachment[];
}

export type ItemKind = "file" | "document";

export interface Item {
  path: string;
  kind: ItemKind;
  version: number;
  size: number;
  updated_at: string;
  updated_by_agent_id: string | null;
  media_type?: string;
  pages?: number | null;
}

export interface ItemContent extends Item {
  content: string;
  part?: number;
  parts?: number;
  outline?: string[];
  next_section?: string | null;
}

export interface ItemVersion {
  version: number;
  kind: ItemKind;
  change: string;
  deleted: boolean;
  author: "agent" | "user";
  author_agent_id: string | null;
  size: number;
  created_at: string;
  content?: string | null;
}

export interface WriteResult {
  store_id: string;
  path: string;
  version: number;
  size: number;
}

export interface UploadResult extends WriteResult {
  media_type: string;
  pages: number | null;
  sections: number;
  passages: number;
}

export interface EmbeddingModel {
  provider: Provider;
  model: string;
  dimensions?: number | null;
}

export interface KnowledgeStatus {
  search_mode: "hybrid" | "keyword";
  fallback_reason: string | null;
  sqlite_vec: boolean;
  embedding: EmbeddingModel | null;
  pending_embeddings: number;
  last_error: string | null;
  entity_catalog: number;
}

// Settings (docs/api.md, "Settings").

export interface KeyInfo {
  set: boolean;
  hint: string | null;
  source: "settings" | "environment" | "options" | null;
}

export interface PriceOverride {
  input: number;
  output?: number;
  cache_read?: number | null;
  cache_write?: number | null;
}

export interface ApprovalSettings {
  approvers: string[];
  notify_services: Record<string, string[]>;
  timeout_seconds: number;
}

export interface SettingValues {
  installation_daily_budget_usd: number | null;
  agent_daily_budget_usd: number | null;
  price_overrides: Record<string, PriceOverride> | null;
  genai_prices_auto_update: boolean | null;
  decision_models: ModelRef[] | null;
  approvals: ApprovalSettings | null;
}

export interface SettingsResponse {
  keys: Record<Provider, KeyInfo>;
  settings: SettingValues;
  defaults: SettingValues;
  built_in_decision_models: ModelRef[];
}

export interface HaUser {
  id: string;
  name: string;
  username: string | null;
  is_admin: boolean | null;
}

export interface HaUsers {
  source: "auth" | "persons" | null;
  reason: string | null;
  users: HaUser[];
}

// Inspect (docs/api.md, "Inspect", "Rules" and "Approvals")

export type InvocationStatus =
  "queued" | "running" | "ok" | "fallback" | "error" | "cancelled";
export type InvocationPath = "rule" | "agent_turn" | "fallback";

export interface InvocationSummary {
  id: string;
  agent_id: string;
  source: string;
  source_key: string;
  conversation_id: string | null;
  dry_run: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  status: InvocationStatus;
  path: InvocationPath | null;
  fallback_reason: string | null;
  error_code: string | null;
  agent_version: number | null;
  rule_id: string | null;
  rule_version: number | null;
  turn_id: string | null;
  cost_usd: number;
}

export interface InvocationFilters {
  agent_id?: string;
  source?: string;
  path?: string;
  status?: string;
  q?: string;
}

export interface AuditEntry {
  id: number;
  created_at: string;
  finished_at: string | null;
  agent_id: string;
  agent_version: number;
  invocation_id: string | null;
  tool: string;
  level: string;
  request: unknown;
  decision: "allowed" | "denied";
  reason_code: string | null;
  reason: string | null;
  status: string;
  error: string | null;
  context_id: string | null;
  previous: unknown;
  previous_note: string | null;
}

export interface Tokens {
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  output_tokens: number;
}

export interface TraceToolCall {
  seq: number;
  created_at: string;
  tool: string;
  arguments: unknown;
  ok: boolean;
  error_code: string | null;
  result: unknown;
  result_chars: number;
  cut: boolean;
  scope_decision: "allowed" | "denied";
  audit: AuditEntry | null;
}

export interface TraceCompaction {
  id: number;
  created_at: string;
  kind: string;
  detail: unknown;
  summary: string | null;
}

export interface TraceTurn {
  id: string;
  agent_version: number;
  source: string;
  dry_run: boolean;
  started_at: string;
  finished_at: string | null;
  status: string;
  stop_reason: string | null;
  error: string | null;
  input: unknown;
  prompt: string;
  result: unknown;
  messages: unknown;
  usage: Tokens & { requests: number; tool_calls: number };
  tool_calls: TraceToolCall[];
  compactions: TraceCompaction[];
}

export interface ProviderCall extends Tokens {
  id: number;
  started_at: string;
  provider: string;
  call_type: "chat" | "decision" | "embedding";
  model: string;
  status: string;
  error: string | null;
  cost_usd: number;
  cost_source: string;
}

export interface Trace {
  invocation: InvocationSummary & {
    request: unknown;
    result: unknown;
    error: string | null;
  };
  turns: TraceTurn[];
  audit: AuditEntry[];
  provider_calls: ProviderCall[];
  trials: {
    id: string;
    rule_id: string;
    version: number;
    status: string;
    rule_result: unknown;
    agent_result: unknown;
    detail: string | null;
    cost_usd: number;
  }[];
  approvals: { id: string; tool: string; status: string }[];
  cost: Tokens & { total_usd: number };
}

export interface ScopeView {
  all: boolean;
  entities: string[];
  domains: string[];
}

export interface Permissions {
  agent_id: string;
  version: number;
  registry_stale: boolean;
  ha: {
    level: HaLevel;
    services: string[];
    outputs: string[];
    read?: ScopeView;
    control?: ScopeView;
    unavailable?: string;
  };
  tools: { name: string; group: string }[];
  knowledge: { store_id: string; access: StoreAccess }[];
  approval: { tools: string[]; services: string[]; entities: string[] };
  denied: AuditEntry[];
}

export interface ScheduleRun {
  due_at: string;
  at: string;
  status: string;
  missed_count?: number;
  invocation_id: string | null;
  result_status?: string | null;
  result_code?: string | null;
}

export interface ScheduleItem {
  id: string;
  agent_id: string;
  kind: "schedule" | "subscription";
  spec: unknown;
  enabled: boolean;
  run_at: string | null;
  created_at: string;
  updated_at: string;
  last_run: ScheduleRun | null;
  last_event: {
    event_type: string;
    entity_id: string | null;
    origin: string;
    received_at: string;
    outcome: string;
    detail: string | null;
    invocation_id: string | null;
  } | null;
}

export interface Schedules {
  items: ScheduleItem[];
  reviews: (ScheduleRun & { agent_id: string })[];
}

export interface CostTotals extends Tokens {
  calls: number;
  cost_usd: number;
  embedding_cost_usd: number;
}

export interface CostReport {
  since: string;
  total: CostTotals;
  by_agent: (CostTotals & { agent_id: string | null })[];
  by_day: (CostTotals & { day: string })[];
  by_path: (CostTotals & { path: string })[];
  by_type: (CostTotals & { call_type: string })[];
}

export type RuleState = "draft" | "active" | "off";

export interface RuleSummary {
  id: string;
  position: number;
  state: RuleState;
  current_version: number;
  latest_version: number;
  updated_at: string;
  spec: Record<string, unknown>;
}

export interface Backtest {
  id: string;
  rule_id: string;
  version: number;
  status: string;
  started_at: string;
  finished_at: string | null;
  candidates: number;
  matched: number;
  compared: number;
  agreed: number;
  disagreed: number;
  declined: number;
  errors: number;
  skipped: number;
  agreement_rate: number | null;
  cost_usd: number;
  error: string | null;
  cases?: {
    seq: number;
    invocation_id: string;
    invocation_at: string;
    outcome: string;
    rule_result: unknown;
    agent_result: unknown;
    detail: string | null;
  }[];
}

export interface RuleVersionStats {
  version: number;
  created_at: string;
  matched: number;
  answered: number;
  declined: number;
  cache_hits: number;
  errors: number;
  cost_usd: number;
  last_used_at: string | null;
  last_error: string | null;
  backtest?: {
    runs: number;
    cost_usd: number;
    agreement_rate: number | null;
  };
  trial?: {
    runs: number;
    agreed: number;
    disagreed: number;
    errors: number;
    budget: number;
    agreement_rate: number | null;
    cost_usd: number;
  };
}

export interface RuleDetail {
  id: string;
  state: RuleState;
  current_version: number;
  latest_version: number;
  deleted: boolean;
  spec: Record<string, unknown>;
  versions: RuleVersionStats[];
}

export interface Trial {
  id: string;
  version: number;
  invocation_id: string;
  created_at: string;
  status: string;
  rule_result: unknown;
  agent_result: unknown;
  detail: string | null;
  cost_usd: number;
}

export type ApprovalStatus =
  | "pending"
  | "accepted"
  | "rejected"
  | "timeout"
  | "failed"
  | "cancelled"
  | "expired";

export interface ApprovalRequest {
  id: string;
  status: ApprovalStatus;
  agent_id: string;
  agent_version: number;
  invocation_id: string | null;
  tool: string;
  summary: string;
  request: unknown;
  approvers: string[];
  targets: string[];
  problems: string[];
  created_at: string;
  expires_at: string;
  finished_at: string | null;
  answered_by: string | null;
  answered_via: string | null;
  reason: string | null;
  can_answer?: boolean;
  answers?: {
    received_at: string;
    decision: string;
    user_id: string | null;
    via: string;
    outcome: string;
  }[];
}
