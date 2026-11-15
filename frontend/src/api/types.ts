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
  budget: { daily_usd: number; max_tool_calls: number; max_tokens: number };
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
