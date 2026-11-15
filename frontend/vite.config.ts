import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// The app serves the build under the Ingress base path. With base "./",
// every asset URL in index.html is relative, and the <base> element that
// the app adds resolves it.
const appUrl = process.env.HAGENT_APP_URL ?? "http://127.0.0.1:8099";

export default defineConfig({
  base: "./",
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": { target: appUrl, ws: true, changeOrigin: false },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
