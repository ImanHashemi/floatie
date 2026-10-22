import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";
import "./check-toolchain.mjs";

const ui = fileURLToPath(new URL("..", import.meta.url));
const shipped = join(ui, "dist");
const temporary = mkdtempSync(join(tmpdir(), "floatie-ui-dist-"));

function files(directory, prefix = "") {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const name = prefix + entry.name;
    if (entry.isSymbolicLink()) throw new Error(`Build contains a symlink: ${name}`);
    if (entry.isDirectory()) return files(join(directory, entry.name), name + "/");
    if (!entry.isFile()) throw new Error(`Build contains a non-file: ${name}`);
    return [name];
  }).sort();
}

try {
  const rebuilt = join(temporary, "dist");
  execFileSync("npm", ["run", "build", "--", "--outDir", rebuilt, "--emptyOutDir"], {
    cwd: ui, stdio: "inherit",
  });
  const expected = files(rebuilt);
  const actual = files(shipped);
  if (JSON.stringify(expected) !== JSON.stringify(actual)) {
    throw new Error("dist file set differs from the build (missing or extra artifact). Run the UI build and stage dist.");
  }
  for (const name of expected) {
    if (!readFileSync(join(rebuilt, name)).equals(readFileSync(join(shipped, name)))) {
      throw new Error(`dist bytes differ: ${name}. Run the UI build and stage dist.`);
    }
  }
  const root = execFileSync("git", ["rev-parse", "--show-toplevel"], { cwd: ui, encoding: "utf8" }).trim();
  const prefix = relative(root, shipped).split(sep).join("/") + "/";
  const tracked = execFileSync("git", ["ls-files", "--full-name", "-z", "--", "dist"], {
    cwd: ui, encoding: "utf8",
  }).split("\0").filter(Boolean).map((name) => name.slice(prefix.length)).sort();
  if (JSON.stringify(tracked) !== JSON.stringify(actual)) {
    throw new Error("dist tracking differs: every shipped artifact must be tracked, including ignored files.");
  }
  console.log(`dist matches the clean build: ${actual.length} tracked files.`);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
