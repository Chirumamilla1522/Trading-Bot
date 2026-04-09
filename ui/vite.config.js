import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

/** Config file lives in `ui/` — pin root so Vite resolves `index.html` + `node_modules/react` when cwd is the repo root or another folder. */
const __dirname = path.dirname(fileURLToPath(import.meta.url));

const host = process.env.TAURI_DEV_HOST;

export default defineConfig({
  root: __dirname,
  plugins: [react()],
  resolve: {
    dedupe: ["react", "react-dom"],
  },
  optimizeDeps: {
    include: ["react", "react-dom", "react/jsx-runtime", "react/jsx-dev-runtime"],
  },
  // Tauri expects a fixed port; fail if it is not available
  server: {
    host: host || false,
    port: 1420,
    strictPort: true,
    // Allow the Tauri app origin
    hmr: host
      ? { protocol: "ws", host, port: 1421 }
      : undefined,
  },
  // Produce relative asset paths so Tauri's asset protocol works
  base: "./",
  build: {
    outDir: "dist",
    target: ["es2021", "chrome105", "safari15"],
    // Tauri uses Rust to inline the bundle; minify is optional
    minify: !process.env.TAURI_DEBUG ? "esbuild" : false,
    sourcemap: !!process.env.TAURI_DEBUG,
  },
});
