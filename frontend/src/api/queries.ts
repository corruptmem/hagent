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
