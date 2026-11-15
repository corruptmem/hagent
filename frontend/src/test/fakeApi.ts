import { vi } from "vitest";
import type {
  Agent,
  AgentDefinition,
  Catalog,
  VersionSummary,
} from "../api/types";
import defaults from "./fixtures/defaults.json";

export const DEFAULTS = defaults.definition as AgentDefinition;
export const AGENT_ID = "a".repeat(32);
export const STORE_ID = "b".repeat(32);

export interface Call {
  method: string;
  path: string;
  body: unknown;
  /** A body that is not text, for example an uploaded file. */
  raw?: unknown;
  headers?: Record<string, string>;
}

type Reply = { status: number; body: unknown } | undefined;
export type Handler = (call: Call) => Reply;

export function makeAgent(change: Partial<Agent> = {}): Agent {
  return {
    id: AGENT_ID,
    name: "Test agent",
    enabled: true,
    version: 2,
    created_at: "2026-11-14T10:00:00+00:00",
    updated_at: "2026-11-14T11:00:00+00:00",
    private_store: { id: STORE_ID, name: "test_agent_private" },
    definition: {
      ...DEFAULTS,
      name: "Test agent",
      knowledge: [
        { store_id: STORE_ID, access: "read_write", inject_main: true },
      ],
    },
    ...change,
  };
}

export const VERSIONS: VersionSummary[] = [
  {
    version: 2,
    change: "edit",
    source_agent_id: null,
    source_version: null,
    created_at: "2026-11-14T11:00:00+00:00",
  },
  {
    version: 1,
    change: "create",
    source_agent_id: null,
    source_version: null,
    created_at: "2026-11-14T10:00:00+00:00",
  },
];

export const CATALOG: Catalog = {
  stale: false,
  entities: [
    {
      entity_id: "light.test_lamp",
      name: "Test lamp",
      domain: "light",
      state: "on",
      device_id: "dev1",
      area_id: "area1",
      labels: [],
      disabled: false,
    },
    {
      entity_id: "sensor.test_temperature",
      name: "Test temperature",
      domain: "sensor",
      state: "19.5",
      device_id: null,
      area_id: null,
      labels: [],
      disabled: false,
    },
  ],
  devices: [{ id: "dev1", name: "Lamp device", area_id: "area1", labels: [] }],
  areas: [{ id: "area1", name: "Room A", floor_id: null }],
  floors: [],
  labels: [{ id: "lbl1", name: "Test label" }],
  domains: ["light", "sensor"],
  services: ["light.turn_off", "light.turn_on"],
};

/**
 * Replace `fetch` with a fake management API. `handler` answers first; the
 * default routes answer the rest. Every call is recorded in `calls`.
 */
export function fakeApi(handler: Handler = () => undefined, base = "") {
  const calls: Call[] = [];
  const agent = makeAgent();
  const defaultsRoute = (call: Call): NonNullable<Reply> => {
    const key = `${call.method} ${call.path}`;
    switch (key) {
      case "GET /api/agents":
        return {
          status: 200,
          body: { agents: [{ ...agent, definition: undefined }] },
        };
      case "GET /api/agents/defaults":
        return { status: 200, body: { definition: DEFAULTS } };
      case `GET /api/agents/${AGENT_ID}`:
        return { status: 200, body: agent };
      case `GET /api/agents/${AGENT_ID}/versions`:
        return { status: 200, body: { versions: VERSIONS } };
      case "GET /api/ha/catalog":
        return { status: 200, body: CATALOG };
      default:
        return {
          status: 404,
          body: { error: { code: "not_found", message: "no route" } },
        };
    }
  };
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : String(input));
    const call: Call = {
      method: init?.method ?? "GET",
      path: url.pathname + url.search,
      body: typeof init?.body === "string" ? JSON.parse(init.body) : undefined,
    };
    if (init?.body != null && typeof init.body !== "string") {
      call.raw = init.body;
      call.headers = init.headers as Record<string, string>;
    }
    calls.push(call);
    const local = { ...call, path: call.path.slice(base.length) };
    const reply = call.path.startsWith(base)
      ? (handler(local) ?? defaultsRoute(local))
      : { status: 404, body: null };
    return Promise.resolve(
      new Response(JSON.stringify(reply.body), {
        status: reply.status,
        headers: { "Content-Type": "application/json" },
      }),
    );
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, agent };
}
