// Assemble the desktop frontend from the web workbench, so both ship the same pages and scripts.
//   workbench/static/login.html -> dist/index.html      (layer 1: sign-in and registration)
//   workbench/static/home.html  -> dist/home.html       (layer 2: project overview)
//   workbench/static/index.html -> dist/workspace.html  (layer 3: project workspace)
//   workbench/static/**         -> dist/static/**       (scripts, styles, vendored libraries, fonts)
import { cpSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(root, "..", "workbench", "static");
const dist = join(root, "dist");
const PAGES = { "login.html": "index.html", "home.html": "home.html", "index.html": "workspace.html" };

rmSync(dist, { recursive: true, force: true });
mkdirSync(dist, { recursive: true });
cpSync(source, join(dist, "static"), {
  recursive: true,
  filter: (path) => !path.endsWith(".html") && !path.endsWith(".md") && !path.endsWith(".DS_Store"),
});
for (const [from, to] of Object.entries(PAGES)) {
  const html = readFileSync(join(source, from), "utf8");
  if (!html.includes('type="module"')) throw new Error(`${from}: expected a module script`);
  writeFileSync(join(dist, to), html);
}
console.log(`assembled ${Object.keys(PAGES).length} pages into ${dist}`);
