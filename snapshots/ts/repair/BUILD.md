# Build

Run these from the workspace root, with the offline npm kit on PATH (Node 26). All four are required:

```sh
npm ci --offline
npx tsc -p tsconfig.json
node dist/build/bundle.js
node dist/build/warm.js
```

1. Install the locked dependencies into `node_modules/`.
2. Compile `src/**/*.ts` into `dist/` (CommonJS).
3. Write one bundle per command: `dist/tinycoder-files.js`, `dist/tinycoder-shell.js` and
   `dist/tinycoder-run.js` (`src/build/bundle.ts`).
4. Write their V8 code caches, `dist/tinycoder-*.cache` (`src/build/warm.ts`). This runs each command once,
   offline, in a temporary directory. It needs `ls` on PATH (it calls `run_command ls`) and makes no network
   connection.

`npm run build` runs steps 2–4. `bin/tinycoder` is a checked-in Node launcher (not generated). It loads the
bundle for the command with its code cache. If the bundles are missing (only steps 1–2 were run), it falls back
to `dist/main.js`. That is slower but behaves the same. A missing or rejected cache only slows startup.
