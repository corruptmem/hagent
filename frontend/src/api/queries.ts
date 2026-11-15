import { queryOptions } from "@tanstack/react-query";
import { api } from "./client";

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
