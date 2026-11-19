import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Item, ItemContent, ItemVersion, Store } from "../../api/types";
import { AGENT_ID, STORE_ID, fakeApi, type Call } from "../../test/fakeApi";
import { renderApp } from "../../test/render";
import { FakeWebSocket } from "../../test/setup";

/** The value, or a test failure when it is missing. */
function must<T>(value: T | null | undefined): T {
  if (value == null) throw new Error("missing value");
  return value;
}

const SHARED_ID = "d".repeat(32);
const BASE = `/api/knowledge/stores/${SHARED_ID}`;
const NOW = "2026-11-14T12:00:00+00:00";

function makeStore(change: Partial<Store> = {}): Store {
  return {
    id: SHARED_ID,
    name: "heating",
    description: "Facts about the heating.",
    private: false,
    owner_agent_id: null,
    size_limit: 1_000_000,
    size: 5120,
    files: 2,
    documents: 1,
    created_at: NOW,
    updated_at: NOW,
    attached: [
      {
        agent_id: AGENT_ID,
        agent_name: "Test agent",
        access: "read_write",
        inject_main: false,
      },
    ],
    ...change,
  };
}

const PRIVATE: Store = makeStore({
  id: STORE_ID,
  name: "test_agent_private",
  private: true,
  owner_agent_id: AGENT_ID,
  description: "",
  attached: [
    {
      agent_id: AGENT_ID,
      agent_name: "Test agent",
      access: "read_write",
      inject_main: true,
    },
  ],
});

const ITEMS: Item[] = [
  {
    path: "MAIN.md",
    kind: "file",
    version: 4,
    size: 20,
    updated_at: NOW,
    updated_by_agent_id: AGENT_ID,
  },
  {
    path: "boiler.pdf",
    kind: "document",
    version: 1,
    size: 4000,
    updated_at: NOW,
    updated_by_agent_id: null,
    media_type: "application/pdf",
    pages: 3,
  },
  {
    path: "notes/living room.md",
    kind: "file",
    version: 2,
    size: 10,
    updated_at: NOW,
    updated_by_agent_id: null,
  },
];

const HISTORY: ItemVersion[] = [
  {
    version: 4,
    kind: "file",
    change: "replace",
    deleted: false,
    author: "agent",
    author_agent_id: AGENT_ID,
    size: 20,
    created_at: NOW,
  },
  {
    version: 3,
    kind: "file",
    change: "put",
    deleted: false,
    author: "user",
    author_agent_id: null,
    size: 12,
    created_at: NOW,
  },
];

function content(item: Item, text: string, extra = {}): ItemContent {
  return { ...item, content: text, ...extra };
}

const ok = (body: unknown, status = 200) => ({ status, body });
const notFound = { status: 404, body: { error: { code: "not_found" } } };

/** A fake knowledge API. `extra` answers first. */
function knowledgeApi(extra: (call: Call) => unknown = () => undefined) {
  return fakeApi((call) => {
    const special = extra(call);
    if (special) return special as { status: number; body: unknown };
    const { method, path } = call;
    if (method === "GET" && path === "/api/knowledge/stores")
      return ok({ stores: [makeStore(), PRIVATE] });
    if (method === "GET" && path === BASE) return ok(makeStore());
    if (method === "GET" && path === `${BASE}/items`)
      return ok({ items: ITEMS });
    if (method === "GET" && path === `${BASE}/items/MAIN.md`)
      return ok(content(must(ITEMS[0]), "# Main\nThe boiler."));
    if (method === "GET" && path === `${BASE}/items/boiler.pdf`)
      return ok(content(must(ITEMS[1]), "Page text"));
    if (method === "GET" && path.startsWith(`${BASE}/history/`))
      return ok({ versions: HISTORY });
    return undefined;
  });
}

async function openStore() {
  renderApp(`/knowledge/${SHARED_ID}`);
  await screen.findByRole("heading", { name: "heating" });
}

describe("knowledge list", () => {
  it("shows each store with the agents that attach it", async () => {
    knowledgeApi();
    renderApp("/knowledge");
    const link = await screen.findByRole("link", { name: "heating" });
    expect(link).toHaveAttribute("href", `/knowledge/${SHARED_ID}`);
    const rows = screen.getAllByRole("row");
    const shared = must(rows.find((r) => r.textContent.includes("heating")));
    expect(
      within(shared).getByRole("link", { name: "Test agent" }),
    ).toHaveAttribute("href", `/agents/${AGENT_ID}`);
    expect(shared).toHaveTextContent("read_write");
    const priv = must(
      rows.find((r) => r.textContent.includes("test_agent_private")),
    );
    expect(priv).toHaveTextContent("private");
    expect(priv).toHaveTextContent("main file");
  });

  it("loads the stores again after a knowledge or agents event", async () => {
    const api = knowledgeApi();
    renderApp("/knowledge");
    await screen.findByRole("link", { name: "heating" });
    const count = () =>
      api.calls.filter((c) => c.path === "/api/knowledge/stores").length;
    const before = count();
    const socket = must(FakeWebSocket.instances[0]);
    socket.emit({ topic: "events", action: "ready", id: "" });
    socket.emit({ topic: "knowledge", action: "updated", id: SHARED_ID });
    await waitFor(() => {
      expect(count()).toBe(before + 1);
    });
    socket.emit({ topic: "agents", action: "updated", id: AGENT_ID });
    await waitFor(() => {
      expect(count()).toBe(before + 2);
    });
  });

  it("creates a store and opens it", async () => {
    const api = knowledgeApi((call) =>
      call.method === "POST" && call.path === "/api/knowledge/stores"
        ? ok(makeStore(), 201)
        : undefined,
    );
    const { router } = renderApp("/knowledge");
    await userEvent.click(
      await screen.findByRole("button", { name: "New store" }),
    );
    await userEvent.type(screen.getByLabelText("Name"), "heating");
    await userEvent.type(screen.getByLabelText("Description"), "Facts");
    await userEvent.type(screen.getByLabelText("Size limit (bytes)"), "2000");
    await userEvent.click(screen.getByRole("button", { name: "Create store" }));
    await waitFor(() => {
      expect(router.state.location.pathname).toBe(`/knowledge/${SHARED_ID}`);
    });
    expect(api.calls).toContainEqual({
      method: "POST",
      path: "/api/knowledge/stores",
      body: { name: "heating", description: "Facts", size_limit: 2000 },
    });
  });
});

describe("store view", () => {
  it("edits the name, description and size limit", async () => {
    const api = knowledgeApi((call) =>
      call.method === "PATCH"
        ? ok(makeStore({ name: "boiler", updated_at: "x" }))
        : undefined,
    );
    await openStore();
    const form = screen.getByRole("form", { name: "Store settings" });
    const name = within(form).getByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "boiler");
    const limit = within(form).getByLabelText("Size limit (bytes)");
    await userEvent.clear(limit);
    await userEvent.type(limit, "5000");
    await userEvent.click(
      within(form).getByRole("button", { name: "Save store" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PATCH",
        path: BASE,
        body: { name: "boiler", size_limit: 5000 },
      });
    });
  });

  it("explains why a store in use cannot be deleted", async () => {
    knowledgeApi((call) =>
      call.method === "DELETE" && call.path === BASE
        ? {
            status: 409,
            body: {
              error: {
                code: "store_in_use",
                message: "in use",
                details: { agent_ids: [AGENT_ID] },
              },
            },
          }
        : undefined,
    );
    await openStore();
    await userEvent.click(screen.getByRole("button", { name: "Delete store" }));
    const dialog = screen.getByRole("dialog", { name: "Delete store" });
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete" }),
    );
    expect(
      await within(dialog).findByText(/Detach it from each agent first/),
    ).toBeInTheDocument();
  });

  it("shows the agents that attach the store", async () => {
    knowledgeApi();
    await openStore();
    expect(screen.getByRole("link", { name: "Test agent" })).toHaveAttribute(
      "href",
      `/agents/${AGENT_ID}`,
    );
  });

  it("uploads, replaces and deletes a document", async () => {
    const api = knowledgeApi((call) => {
      if (call.method === "POST" && call.path.startsWith(`${BASE}/documents`))
        return ok(
          {
            store_id: SHARED_ID,
            path: "boiler.pdf",
            version: 1,
            size: 4000,
            media_type: "application/pdf",
            pages: 3,
            sections: 3,
            passages: 5,
          },
          201,
        );
      if (call.method === "PUT" && call.path.startsWith(`${BASE}/documents/`))
        return ok({ store_id: SHARED_ID, path: "boiler.pdf", version: 2 });
      if (call.method === "DELETE") return ok({ version: 2 });
      return undefined;
    });
    await openStore();
    const file = new File(["%PDF"], "boiler.pdf", { type: "application/pdf" });
    await userEvent.upload(screen.getByLabelText("Upload document"), file);
    await waitFor(() => {
      expect(api.calls.find((c) => c.method === "POST")?.path).toBe(
        `${BASE}/documents?name=boiler.pdf`,
      );
    });
    const post = must(api.calls.find((c) => c.method === "POST"));
    expect(post.raw).toBe(file);
    expect(post.headers?.["Content-Type"]).toBe("application/pdf");

    // The upload opens the document. Documents are read-only.
    const panel = await screen.findByRole("region", {
      name: "Item boiler.pdf",
    });
    expect(await within(panel).findByText("Page text")).toBeInTheDocument();
    expect(within(panel).queryByLabelText("File content")).toBeNull();

    const next = new File(["%PDF2"], "boiler-2019.pdf");
    await userEvent.upload(
      within(panel).getByLabelText("Replace document"),
      next,
    );
    await waitFor(() => {
      expect(api.calls.find((c) => c.method === "PUT")?.path).toBe(
        `${BASE}/documents/boiler.pdf`,
      );
    });

    await userEvent.click(
      within(panel).getByRole("button", { name: "Delete" }),
    );
    const dialog = within(panel).getByRole("dialog", { name: "Delete item" });
    expect(dialog).toHaveTextContent("cannot be restored");
    await userEvent.click(
      within(dialog).getByRole("button", { name: "Delete item" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "DELETE",
        path: `${BASE}/items/boiler.pdf?base_version=1`,
        body: undefined,
      });
    });
  });

  it("edits a file with its version and handles a conflict", async () => {
    let conflict = false;
    const api = knowledgeApi((call) => {
      if (call.method === "PUT" && call.path.startsWith(`${BASE}/files/`)) {
        if (conflict)
          return {
            status: 409,
            body: {
              error: {
                code: "version_conflict",
                message: "changed",
                details: { version: 5 },
              },
            },
          };
        return ok({ store_id: SHARED_ID, path: "MAIN.md", version: 5 });
      }
      return undefined;
    });
    await openStore();
    await userEvent.click(
      await screen.findByRole("button", { name: "MAIN.md" }),
    );
    const panel = await screen.findByRole("region", { name: "Item MAIN.md" });
    const editor = await within(panel).findByLabelText("File content");
    expect(editor).toHaveValue("# Main\nThe boiler.");
    await userEvent.type(editor, " New.");
    await userEvent.click(
      within(panel).getByRole("button", { name: "Save file" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PUT",
        path: `${BASE}/files/MAIN.md`,
        body: { content: "# Main\nThe boiler. New.", base_version: 4 },
      });
    });

    conflict = true;
    await userEvent.type(
      await within(panel).findByLabelText("File content"),
      "!",
    );
    await userEvent.click(
      within(panel).getByRole("button", { name: "Save file" }),
    );
    expect(
      await within(panel).findByRole("button", {
        name: "Load the current version",
      }),
    ).toBeInTheDocument();
  });

  it("encodes each part of a path", async () => {
    const api = knowledgeApi((call) =>
      call.path === `${BASE}/items/notes/living%20room.md`
        ? ok(content(must(ITEMS[2]), "Sofa"))
        : undefined,
    );
    await openStore();
    await userEvent.click(
      await screen.findByRole("button", { name: "notes/living room.md" }),
    );
    const panel = await screen.findByRole("region", {
      name: "Item notes/living room.md",
    });
    expect(await within(panel).findByLabelText("File content")).toHaveValue(
      "Sofa",
    );
    expect(api.calls.map((c) => c.path)).toContain(
      `${BASE}/history/notes/living%20room.md`,
    );
  });

  it("does not edit a file that arrives in parts", async () => {
    knowledgeApi((call) =>
      call.path === `${BASE}/items/MAIN.md`
        ? ok(
            content(must(ITEMS[0]), "part one text", {
              part: 1,
              parts: 3,
              outline: [],
              next_section: "part 2",
            }),
          )
        : undefined,
    );
    await openStore();
    await userEvent.click(
      await screen.findByRole("button", { name: "MAIN.md" }),
    );
    const panel = await screen.findByRole("region", { name: "Item MAIN.md" });
    expect(await within(panel).findByText("part one text")).toBeInTheDocument();
    expect(within(panel).queryByLabelText("File content")).toBeNull();
    expect(
      within(panel).queryByRole("button", { name: "Save file" }),
    ).toBeNull();
    expect(panel).toHaveTextContent("This item has 3 parts");
  });

  it("shows an earlier version and restores it", async () => {
    const api = knowledgeApi((call) => {
      if (call.path === `${BASE}/history/MAIN.md?version=3`)
        return ok({ ...HISTORY[1], content: "Old text" });
      if (call.method === "POST" && call.path === `${BASE}/restore`)
        return ok({ store_id: SHARED_ID, path: "MAIN.md", version: 5 });
      return undefined;
    });
    await openStore();
    await userEvent.click(
      await screen.findByRole("button", { name: "MAIN.md" }),
    );
    const panel = await screen.findByRole("region", { name: "Item MAIN.md" });
    // The current version cannot be restored; the earlier one can.
    await within(panel).findByRole("button", { name: "View 3" });
    expect(
      within(panel).queryByRole("button", { name: "Restore 4" }),
    ).toBeNull();
    await userEvent.click(
      within(panel).getByRole("button", { name: "View 3" }),
    );
    expect(await within(panel).findByText("Old text")).toBeInTheDocument();
    await userEvent.click(
      within(panel).getByRole("button", { name: "Restore 3" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "POST",
        path: `${BASE}/restore`,
        body: { path: "MAIN.md", version: 3, base_version: 4 },
      });
    });
  });

  it("restores a deleted file opened by its path", async () => {
    const api = knowledgeApi((call) => {
      if (call.path === `${BASE}/items/rooms.md`) return notFound;
      if (call.path === `${BASE}/history/rooms.md`)
        return ok({
          versions: [
            { ...HISTORY[0], version: 2, change: "delete", deleted: true },
            { ...HISTORY[1], version: 1, change: "create" },
          ],
        });
      if (call.method === "POST" && call.path === `${BASE}/restore`)
        return ok({ store_id: SHARED_ID, path: "rooms.md", version: 3 });
      return undefined;
    });
    await openStore();
    await userEvent.type(screen.getByLabelText("Path to open"), "rooms.md");
    await userEvent.click(screen.getByRole("button", { name: "Open" }));
    const panel = await screen.findByRole("region", { name: "Item rooms.md" });
    expect(
      await within(panel).findByText(/This path has no item now/),
    ).toBeInTheDocument();
    expect(
      within(panel).queryByRole("button", { name: "Restore 2" }),
    ).toBeNull();
    await userEvent.click(
      await within(panel).findByRole("button", { name: "Restore 1" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "POST",
        path: `${BASE}/restore`,
        body: { path: "rooms.md", version: 1 },
      });
    });
  });

  it("creates a file", async () => {
    const api = knowledgeApi((call) =>
      call.method === "PUT"
        ? ok({ store_id: SHARED_ID, path: "rooms.md", version: 1 })
        : undefined,
    );
    await openStore();
    await userEvent.click(screen.getByRole("button", { name: "New file" }));
    const form = screen.getByRole("form", { name: "New file" });
    await userEvent.type(within(form).getByLabelText("Path"), "rooms.md");
    await userEvent.type(
      within(form).getByLabelText("Content (Markdown)"),
      "Rooms",
    );
    await userEvent.click(
      within(form).getByRole("button", { name: "Create file" }),
    );
    await waitFor(() => {
      expect(api.calls).toContainEqual({
        method: "PUT",
        path: `${BASE}/files/rooms.md`,
        body: { content: "Rooms" },
      });
    });
  });
});
