// The Ingress base path.
//
// The app adds <base href="<X-Ingress-Path>/"> to index.html. The router
// uses its path as the base path, and every API URL is relative to it.
// Without the element (the Vite dev server), the base is the site root.

function baseHref(): string {
  const element = document.querySelector("base");
  return element ? element.href : `${window.location.origin}/`;
}

/** The router base path, without a final slash ("" or "/api/hassio_ingress/x"). */
export function routerBasePath(): string {
  return new URL(baseHref()).pathname.replace(/\/+$/, "");
}

/** The absolute URL of a relative app path, for example "api/agents". */
export function appUrl(path: string): URL {
  return new URL(path.replace(/^\/+/, ""), baseHref());
}

/** The WebSocket URL of a relative app path. */
export function wsUrl(path: string): string {
  const url = appUrl(path);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
