import { appUrl } from "../base";
import type {
  Agent,
  AgentDefinition,
  AgentSummary,
  Catalog,
  DeleteChoice,
  Diff,
  TestRequest,
  TestResponse,
  ValidationIssue,
  Version,
  VersionSummary,
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
};
