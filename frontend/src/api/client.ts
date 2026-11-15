import { appUrl } from "../base";
import type {
  Agent,
  AgentDefinition,
  AgentSummary,
  Catalog,
  DeleteChoice,
  Diff,
  EmbeddingModel,
  Item,
  ItemContent,
  ItemVersion,
  KnowledgeStatus,
  Provider,
  SettingValues,
  SettingsResponse,
  Store,
  TestRequest,
  TestResponse,
  UploadResult,
  ValidationIssue,
  Version,
  VersionSummary,
  WriteResult,
} from "./types";

/** An error response of the management API (docs/api.md, "Errors"). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details: unknown;

  constructor(
    status: number,
    code: string,
    message: string,
    details?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  /** The validation issues of a `definition_invalid` error. */
  get issues(): ValidationIssue[] {
    return this.code === "definition_invalid" && Array.isArray(this.details)
      ? (this.details as ValidationIssue[])
      : [];
  }
}

interface ErrorBody {
  error?: { code?: string; message?: string; details?: unknown };
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const init: RequestInit = { method, headers };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    headers["Content-Type"] = "application/json";
  }
  return send<T>(path, init);
}

/**
 * Send a file as the raw request body (document upload). The browser sets
 * the content type from the file; with no type, the app uses the file
 * extension.
 */
async function upload<T>(method: string, path: string, file: Blob): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (file.type) headers["Content-Type"] = file.type;
  return send<T>(path, { method, headers, body: file });
}

async function send<T>(path: string, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(appUrl(path), init);
  } catch (err) {
    throw new ApiError(
      0,
      "network_error",
      `The app is not reachable: ${String(err)}`,
    );
  }
  const text = await response.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = null;
    }
  }
  if (!response.ok) {
    const error = (data as ErrorBody | null)?.error;
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? `HTTP ${response.status}`,
      error?.details,
    );
  }
  return data as T;
}

const agentPath = (id: string) => `api/agents/${encodeURIComponent(id)}`;
const storePath = (id: string) =>
  `api/knowledge/stores/${encodeURIComponent(id)}`;

/** Encode each part of an item path, and keep the `/` between parts. */
export function itemPath(path: string): string {
  return path.split("/").map(encodeURIComponent).join("/");
}

export interface StoreChange {
  name?: string;
  description?: string;
  size_limit?: number;
}

export const api = {
  listAgents: () =>
    request<{ agents: AgentSummary[] }>("GET", "api/agents").then(
      (r) => r.agents,
    ),
  getAgent: (id: string) => request<Agent>("GET", agentPath(id)),
  defaults: () =>
    request<{ definition: AgentDefinition }>("GET", "api/agents/defaults").then(
      (r) => r.definition,
    ),
  createAgent: (definition: AgentDefinition) =>
    request<Agent>("POST", "api/agents", { definition }),
  updateAgent: (id: string, definition: AgentDefinition, baseVersion: number) =>
    request<Agent>("PUT", agentPath(id), {
      definition,
      base_version: baseVersion,
    }),
  setEnabled: (id: string, enabled: boolean) =>
    request<Agent>("PUT", `${agentPath(id)}/enabled`, { enabled }),
  copyAgent: (id: string, name?: string) =>
    request<Agent>("POST", `${agentPath(id)}/copy`, name ? { name } : {}),
  deleteAgent: (id: string, privateStore: DeleteChoice) =>
    request<{ deleted: boolean }>(
      "DELETE",
      `${agentPath(id)}?private_store=${privateStore}`,
    ),
  listVersions: (id: string) =>
    request<{ versions: VersionSummary[] }>(
      "GET",
      `${agentPath(id)}/versions`,
    ).then((r) => r.versions),
  getVersion: (id: string, version: number) =>
    request<Version>("GET", `${agentPath(id)}/versions/${version}`),
  diff: (id: string, from: number, to: number) =>
    request<Diff>("GET", `${agentPath(id)}/diff?from=${from}&to=${to}`),
  restore: (id: string, version: number, baseVersion: number) =>
    request<Agent>("POST", `${agentPath(id)}/versions/${version}/restore`, {
      base_version: baseVersion,
    }),
  testInvocation: (id: string, body: TestRequest) =>
    request<TestResponse>("POST", `${agentPath(id)}/test`, body),
  catalog: () => request<Catalog>("GET", "api/ha/catalog"),

  // Knowledge stores
  listStores: () =>
    request<{ stores: Store[] }>("GET", "api/knowledge/stores").then(
      (r) => r.stores,
    ),
  getStore: (id: string) => request<Store>("GET", storePath(id)),
  createStore: (body: StoreChange & { name: string }) =>
    request<Store>("POST", "api/knowledge/stores", body),
  updateStore: (id: string, body: StoreChange) =>
    request<Store>("PATCH", storePath(id), body),
  deleteStore: (id: string) =>
    request<{ deleted: string }>("DELETE", storePath(id)),
  listItems: (id: string) =>
    request<{ items: Item[] }>("GET", `${storePath(id)}/items`).then(
      (r) => r.items,
    ),
  readItem: (id: string, path: string, section?: string) =>
    request<ItemContent>(
      "GET",
      `${storePath(id)}/items/${itemPath(path)}${
        section ? `?section=${encodeURIComponent(section)}` : ""
      }`,
    ),
  putFile: (id: string, path: string, content: string, baseVersion?: number) =>
    request<WriteResult>("PUT", `${storePath(id)}/files/${itemPath(path)}`, {
      content,
      ...(baseVersion === undefined ? {} : { base_version: baseVersion }),
    }),
  deleteItem: (id: string, path: string, baseVersion?: number) =>
    request<{ version: number }>(
      "DELETE",
      `${storePath(id)}/items/${itemPath(path)}${
        baseVersion === undefined ? "" : `?base_version=${baseVersion}`
      }`,
    ),
  history: (id: string, path: string) =>
    request<{ versions: ItemVersion[] }>(
      "GET",
      `${storePath(id)}/history/${itemPath(path)}`,
    ).then((r) => r.versions),
  historyVersion: (id: string, path: string, version: number) =>
    request<ItemVersion>(
      "GET",
      `${storePath(id)}/history/${itemPath(path)}?version=${version}`,
    ),
  restoreItem: (
    id: string,
    path: string,
    version: number,
    baseVersion?: number,
  ) =>
    request<WriteResult>("POST", `${storePath(id)}/restore`, {
      path,
      version,
      ...(baseVersion === undefined ? {} : { base_version: baseVersion }),
    }),
  uploadDocument: (id: string, file: File) =>
    upload<UploadResult>(
      "POST",
      `${storePath(id)}/documents?name=${encodeURIComponent(file.name)}`,
      file,
    ),
  replaceDocument: (id: string, path: string, file: File) =>
    upload<UploadResult>(
      "PUT",
      `${storePath(id)}/documents/${itemPath(path)}`,
      file,
    ),
  knowledgeStatus: () =>
    request<KnowledgeStatus>("GET", "api/knowledge/status"),
  knowledgeSettings: () =>
    request<{ embedding: EmbeddingModel | null }>(
      "GET",
      "api/knowledge/settings",
    ),
  setEmbedding: (embedding: EmbeddingModel | null) =>
    request<{ embedding: EmbeddingModel | null }>(
      "PUT",
      "api/knowledge/settings",
      { embedding },
    ),

  // Settings
  settings: () => request<SettingsResponse>("GET", "api/settings"),
  patchSettings: (changes: Partial<SettingValues>) =>
    request<SettingsResponse>("PATCH", "api/settings", changes),
  setKey: (provider: Provider, key: string) =>
    request<SettingsResponse>("PUT", `api/settings/keys/${provider}`, { key }),
  deleteKey: (provider: Provider) =>
    request<SettingsResponse>("DELETE", `api/settings/keys/${provider}`),
};
