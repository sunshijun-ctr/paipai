# web/ — frontend bundle

Vite-built ESM bundle that lives next to FastAPI in `app/api/static/dist/`.
Today it ships **vanilla JS** (no React, no TS, no Tailwind). The point is
to get bundling, content-hashing, and a manifest in place so Phase 2.3 of
[`../docs/frontend-migration.md`](../docs/frontend-migration.md) can pull
features out of `index.html` one at a time, and Phase 3 can swap to React
without re-doing the build wiring.

## Install once

```bash
cd web && npm install
```

## Build

```bash
npm run build
```

Output: `../app/api/static/dist/assets/main-<hash>.js` + a manifest at
`../app/api/static/dist/.vite/manifest.json`. FastAPI reads the manifest
at startup and injects the hashed path into `index.html` via the
`{{JS_ENTRY_TAG}}` template placeholder.

If the manifest is missing, FastAPI logs a warning and renders an HTML
comment in place of the bundle — the legacy inline scripts still run, so
the site is never broken because someone forgot `npm run build`.

## Dev iteration

```bash
npm run dev          # vite build --watch
```

Each save triggers a sub-second rebuild. FastAPI sees the new manifest
on its next request (no uvicorn restart needed because the manifest is
read per-request when in dev — see `_vite_entry` in `app/api/server.py`).
The version query string changes too (`?v=<git-sha>` or mtime hash),
busting browser caches automatically.

## Adding a new module

1. Drop a file under `src/your-feature.js`.
2. `import { yourPublicFn } from "./your-feature.js"` in `src/main.js`.
3. `window.yourPublicFn = yourPublicFn` if the legacy inline code calls it
   by name (Phase 2.3 "island" pattern).
4. `npm run build`.
5. Delete the corresponding inline code from `app/api/static/index.html`.

## What's already moved

- `src/research-plan-card.js` — HITL plan-approval card (Phase D)

## What's NOT here

- React / TS / Tailwind — that's Phase 3. The Vite config is set up so
  switching `src/main.js` to React is a one-line change to package.json
  (`npm install react react-dom @vitejs/plugin-react`) plus adding the
  plugin to `vite.config.js`.
- Dev server with HMR — we use `vite build --watch`, not `vite dev`,
  because FastAPI is the only origin and we don't need a separate
  hot-reload server. If iteration speed becomes a bottleneck, switch
  to a `vite dev` proxy setup.
