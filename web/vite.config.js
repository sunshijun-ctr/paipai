// Vite config for the paipai frontend bundle.
//
// What this gets us today:
//   - Content-hashed output → cache busting per module without manual ?v=
//   - Manifest JSON → server-side template can map entry → real filename
//   - Tree-shaking + minify out of the box
//   - HMR via `npm run dev` (`vite build --watch`) when iterating
//
// What this does NOT change yet:
//   - We still ship vanilla JS modules (no React / TS / Tailwind).
//     Phase 3 swaps src/ over to React; this config keeps working.
//
// Output lands inside FastAPI's static dir so a `npm run build` + uvicorn
// reload is all that's needed — no Caddy alias, no separate web server.

import { defineConfig } from "vite";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const STATIC_DIR = path.resolve(__dirname, "../app/api/static");

export default defineConfig({
  root: __dirname,

  build: {
    // Bundle output lands under app/api/static/dist so FastAPI's
    // existing StaticFiles mount serves it at /static/dist/*.
    outDir: path.join(STATIC_DIR, "dist"),
    emptyOutDir: true,            // wipe stale hashed files each build
    manifest: ".vite/manifest.json",
    sourcemap: false,             // toggle to "inline" when debugging prod
    target: "es2020",             // covers all evergreen browsers we care about
    cssCodeSplit: true,           // each entry can pull in its own .css

    rollupOptions: {
      input: {
        // Each entry here becomes an independently-loaded chunk; index.html
        // can decide which entries to <script> in. Today: one entry. Phase 2.3
        // will add chat / sessions / library / notes / settings.
        main: path.join(__dirname, "src/main.js"),
      },
      output: {
        // Filename templates — Vite's defaults are sensible. We just make
        // the asset path explicit so the manifest is easy to read.
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
      },
    },
  },
});
