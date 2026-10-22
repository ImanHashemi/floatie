import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";

const { engines } = JSON.parse(readFileSync(new URL("../package.json", import.meta.url), "utf8"));
if (process.versions.node !== engines.node) {
  throw new Error(`UI contributors need Node ${engines.node}; found ${process.versions.node}. See ui/README.md.`);
}
const npm = execFileSync("npm", ["--version"], { encoding: "utf8" }).trim();
if (npm !== engines.npm) {
  throw new Error(`UI contributors need npm ${engines.npm}; found ${npm}. See ui/README.md.`);
}
