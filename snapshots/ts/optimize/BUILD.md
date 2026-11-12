# Build

Run these from the workspace root, with the offline npm kit on PATH (Node 26):

```sh
npm ci --offline
npx tsc -p tsconfig.json
```

This installs the locked dependencies into `node_modules/` and compiles `src/*.ts` into `dist/`.
`bin/tinycoder` is a checked-in Node launcher (not generated). It enables Node's compile cache and loads
`dist/main.js`.
