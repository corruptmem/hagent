import { queryOptions } from "@tanstack/react-query";
import { api } from "./client";
import type { InvocationFilters } from "./types";

export const keys = {
  agents: ["agents"] as const,
  agent: (id: string) => ["agents", id] as const,
  versions: (id: string) => ["agents", id, "versions"] as const,
  version: (id: string, version: number) =>
    ["agents", id, "versions", version] as const,
  diff: (id: string, from: number, to: number) =>
    ["agents", id, "diff", from, to] as const,
  defaults: ["defaults"] as const,
  catalog: ["catalog"] as const,
  haUsers: ["ha", "users"] as const,
  knowledge: ["knowledge"] as const,
  stores: ["knowledge", "stores"] as const,
  store: (id: string) => ["knowledge", "stores", id] as const,
  items: (id: string) => ["knowledge", "stores", id, "items"] as const,
  item: (id: string, path: string, section: string) =>
    ["knowledge", "stores", id, "item", path, section] as const,
  history: (id: string, path: string) =>
    ["knowledge", "stores", id, "history", path] as const,
  knowledgeStatus: ["knowledge", "status"] as const,
  knowledgeSettings: ["knowledge", "settings"] as const,
  settings: ["settings"] as const,
  inspect: ["inspect"] as const,
  invocations: (filters: InvocationFilters) =>
    ["inspect", "invocations", filters] as const,
  trace: (id: string) => ["inspect", "trace", id] as const,
  audit: (filters: object) => ["inspect", "audit", filters] as const,
  schedules: (agentId: string) => ["inspect", "schedules", agentId] as const,
  cost: (days: number, agentId: string) =>
    ["inspect", "cost", days, agentId] as const,
  permissions: (agentId: string) =>
    ["inspect", "permissions", agentId] as const,
  rules: ["rules"] as const,
  agentRules: (agentId: string) => ["rules", agentId] as const,
  rule: (agentId: string, ruleId: string) =>
    ["rules", agentId, ruleId] as const,
  approvals: ["approvals"] as const,
  approvalList: (status: string) => ["approvals", "list", status] as const,
  approval: (id: string) => ["approvals", id] as const,
};

export const agentsQuery = () =>
  queryOptions({ queryKey: keys.agents, queryFn: api.listAgents });

export const agentQuery = (id: string) =>
  queryOptions({ queryKey: keys.agent(id), queryFn: () => api.getAgent(id) });

export const versionsQuery = (id: string) =>
  queryOptions({
    queryKey: keys.versions(id),
    queryFn: () => api.listVersions(id),
  });

export const versionQuery = (id: string, version: number) =>
  queryOptions({
    queryKey: keys.version(id, version),
    queryFn: () => api.getVersion(id, version),
    staleTime: Infinity,
  });

export const diffQuery = (id: string, from: number, to: number) =>
  queryOptions({
    queryKey: keys.diff(id, from, to),
    queryFn: () => api.diff(id, from, to),
    staleTime: Infinity,
  });

export const defaultsQuery = () =>
  queryOptions({
    queryKey: keys.defaults,
    queryFn: api.defaults,
    staleTime: Infinity,
  });

export const catalogQuery = () =>
  queryOptions({
    queryKey: keys.catalog,
    queryFn: api.catalog,
    staleTime: 30_000,
  });

export const storesQuery = () =>
  queryOptions({ queryKey: keys.stores, queryFn: api.listStores });

export const storeQuery = (id: string) =>
  queryOptions({ queryKey: keys.store(id), queryFn: () => api.getStore(id) });

export const itemsQuery = (id: string) =>
  queryOptions({ queryKey: keys.items(id), queryFn: () => api.listItems(id) });

export const itemQuery = (id: string, path: string, section = "") =>
  queryOptions({
    queryKey: keys.item(id, path, section),
    queryFn: () => api.readItem(id, path, section || undefined),
  });

export const historyQuery = (id: string, path: string) =>
  queryOptions({
    queryKey: keys.history(id, path),
    queryFn: () => api.history(id, path),
  });

export const knowledgeStatusQuery = () =>
  queryOptions({
    queryKey: keys.knowledgeStatus,
    queryFn: api.knowledgeStatus,
  });

export const knowledgeSettingsQuery = () =>
  queryOptions({
    queryKey: keys.knowledgeSettings,
    queryFn: api.knowledgeSettings,
  });

export const settingsQuery = () =>
  queryOptions({ queryKey: keys.settings, queryFn: api.settings });

export const haUsersQuery = () =>
  queryOptions({
    queryKey: keys.haUsers,
    queryFn: api.haUsers,
    staleTime: 60_000,
  });

export const invocationsQuery = (filters: InvocationFilters) =>
  queryOptions({
    queryKey: keys.invocations(filters),
    queryFn: () => api.invocations(filters),
  });

export const traceQuery = (id: string) =>
  queryOptions({ queryKey: keys.trace(id), queryFn: () => api.trace(id) });

export const auditQuery = (filters: { agent_id?: string; decision?: string }) =>
  queryOptions({
    queryKey: keys.audit(filters),
    queryFn: () => api.audit(filters),
  });

export const schedulesQuery = (agentId: string) =>
  queryOptions({
    queryKey: keys.schedules(agentId),
    queryFn: () => api.schedules(agentId || undefined),
  });

export const costQuery = (days: number, agentId: string) =>
  queryOptions({
    queryKey: keys.cost(days, agentId),
    queryFn: () => api.cost(days, agentId || undefined),
  });

export const permissionsQuery = (agentId: string) =>
  queryOptions({
    queryKey: keys.permissions(agentId),
    queryFn: () => api.permissions(agentId),
  });

export const rulesQuery = (agentId: string) =>
  queryOptions({
    queryKey: keys.agentRules(agentId),
    queryFn: () => api.listRules(agentId),
  });

export const ruleQuery = (agentId: string, ruleId: string) =>
  queryOptions({
    queryKey: keys.rule(agentId, ruleId),
    queryFn: () => api.getRule(agentId, ruleId),
  });

export const backtestsQuery = (agentId: string, ruleId: string) =>
  queryOptions({
    queryKey: [...keys.rule(agentId, ruleId), "backtests"],
    queryFn: () => api.listBacktests(agentId, ruleId),
  });

export const trialsQuery = (agentId: string, ruleId: string) =>
  queryOptions({
    queryKey: [...keys.rule(agentId, ruleId), "trials"],
    queryFn: () => api.listTrials(agentId, ruleId),
  });

export const approvalsQuery = (status: string) =>
  queryOptions({
    queryKey: keys.approvalList(status),
    queryFn: () => api.approvals(status || undefined),
  });

export const approvalQuery = (id: string) =>
  queryOptions({
    queryKey: keys.approval(id),
    queryFn: () => api.approval(id),
  });
