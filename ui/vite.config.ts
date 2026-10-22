import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";

const notices = readFileSync(new URL("./THIRD_PARTY_NOTICES.txt", import.meta.url), "utf8");

export default defineConfig({
  root: fileURLToPath(new URL(".", import.meta.url)),
  base: "/",
  publicDir: false,
  build: {
    outDir: "dist",
    sourcemap: false,
    modulePreload: false,
    cssCodeSplit: false,
    rolldownOptions: {
      external: () => false,
      // Insert after minification so the canonical license bytes stay verbatim.
      output: { codeSplitting: false, postBanner: `/*!\n${notices}*/` },
      plugins: [{
        name: "bundled-only",
        generateBundle(_options, bundle) {
          for (const file of Object.values(bundle)) {
            if (file.type === "chunk" && (file.imports.length || file.dynamicImports.length)) {
              throw new Error("Frontend must be one bundled entry without external imports or lazy chunks.");
            }
          }
        },
      }],
    },
  },
});
