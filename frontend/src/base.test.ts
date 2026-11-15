import { describe, expect, it } from "vitest";
import { appUrl, routerBasePath, wsUrl } from "./base";

function setBase(href: string) {
  const base = document.createElement("base");
  base.href = href;
  document.head.prepend(base);
}

describe("Ingress base path", () => {
  it("uses the <base> element that the app adds", () => {
    setBase("/api/hassio_ingress/abc123/");
    expect(routerBasePath()).toBe("/api/hassio_ingress/abc123");
    expect(appUrl("api/agents").pathname).toBe(
      "/api/hassio_ingress/abc123/api/agents",
    );
    // A leading slash must not escape the base path.
    expect(appUrl("/api/agents").pathname).toBe(
      "/api/hassio_ingress/abc123/api/agents",
    );
    expect(wsUrl("api/events")).toMatch(
      /^ws:\/\/[^/]+\/api\/hassio_ingress\/abc123\/api\/events$/,
    );
  });

  it("uses the site root without a <base> element", () => {
    expect(routerBasePath()).toBe("");
    expect(appUrl("api/agents").pathname).toBe("/api/agents");
  });
});
