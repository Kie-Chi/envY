// Offline factory discovery only: no sessions, models, or user configuration.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { syncBuiltinESMExports } from "node:module";
import childProcess from "node:child_process";
import net from "node:net";
import http from "node:http";
import https from "node:https";
import tls from "node:tls";
import dgram from "node:dgram";

const write = process.stdout.write.bind(process.stdout);
// Extension diagnostics may contain arbitrary data. Emit only our own schema.
process.stdout.write = () => true;
process.stderr.write = () => true;
for (const key of ["log", "warn", "error", "info", "debug"]) console[key] = () => {};
const blocked = () => { throw new Error("Offline probe operation blocked"); };
globalThis.fetch = blocked;
for (const key of ["spawn", "spawnSync", "exec", "execSync", "execFile", "execFileSync", "fork"]) childProcess[key] = blocked;
net.connect = net.createConnection = blocked;
net.Socket.prototype.connect = blocked;
tls.connect = http.request = http.get = https.request = https.get = dgram.createSocket = blocked;
syncBuiltinESMExports();

let root;
const output = { schemaVersion: 1, status: "error", hostVersion: null, extensions: [] };
const safeName = value => typeof value === "string" && /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$/.test(value);
const safeRegistration = value => typeof value === "string" && /^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,127}$/.test(value);
const store = value => {
  if (typeof value !== "string" || !path.isAbsolute(value)) throw new Error("Invalid source");
  const resolved = fs.realpathSync(value);
  if (!resolved.startsWith("/nix/store/")) throw new Error("Invalid source");
  return resolved;
};
const version = value => /^\d+\.\d+\.\d+$/.test(value) ? value.split(".").map(Number) : null;
function guard(source, hostVersion) {
  const result = { status: "unknown" };
  const file = path.join(source, "src/pi-version.ts");
  if (!fs.statSync(source).isDirectory() || !fs.existsSync(file)) return result;
  const text = fs.readFileSync(file, "utf8").replace(
    /("(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g,
    (match, literal) => literal ?? " ",
  );
  // Match only a literal published declaration, never evaluate extension code.
  const matches = [...text.matchAll(/^\s*export\s+const\s+MIN_PI_VERSION\s*=\s*["'](\d+\.\d+\.\d+)["']\s*;/gm)];
  if (matches.length !== 1) return result;
  const minimum = matches[0][1];
  const actual = version(hostVersion);
  if (!actual) return result;
  const required = version(minimum);
  const comparison = actual.map((number, i) => number - required[i]).find(number => number !== 0) ?? 0;
  return { status: comparison >= 0 ? "ok" : "error", minimum, declaration: "src/pi-version.ts:MIN_PI_VERSION" };
}
try {
  const input = fs.readFileSync(0, "utf8");
  if (input.length > 2_000_000) throw new Error("Invalid input");
  const data = JSON.parse(input);
  if (data.schemaVersion !== 1 || !Array.isArray(data.extensions) || data.extensions.length > 256) throw new Error("Invalid input");
  const modules = store(data.pi.nodeModules);
  root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-doctor-"));
  const agentDir = path.join(root, "agent");
  const cwd = path.join(root, "project");
  fs.mkdirSync(agentDir);
  fs.mkdirSync(cwd);
  // All host paths are redirected before importing Pi (which caches agentDir).
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, {
    HOME: root, TMPDIR: root, XDG_CONFIG_HOME: root, XDG_CACHE_HOME: root,
    PI_CODING_AGENT_DIR: agentDir, PI_CODING_AGENT_SESSION_DIR: path.join(root, "sessions"),
    JITI_FS_CACHE: "false", NO_COLOR: "1",
  });
  process.chdir(cwd);
  const hostRoot = path.join(modules, "@earendil-works/pi-coding-agent");
  output.hostVersion = JSON.parse(fs.readFileSync(path.join(hostRoot, "package.json"), "utf8")).version;
  const { discoverAndLoadExtensions } = await import(pathToFileURL(path.join(hostRoot, "dist/core/extensions/loader.js")));
  for (let i = 0; i < data.extensions.length; i++) {
    const entry = data.extensions[i];
    if (!safeName(entry.name) || !entry.name.startsWith("envy-")) throw new Error("Invalid extension");
    const source = store(entry.source);
    const result = { name: entry.name, status: "error", tools: [], commands: [], guard: guard(source, output.hostVersion) };
    output.extensions.push(result);
    if (result.guard.status === "error") {
      result.status = "unsupported";
      continue;
    }
    let packageName;
    if (fs.statSync(source).isDirectory() && fs.existsSync(path.join(source, "package.json"))) {
      packageName = JSON.parse(fs.readFileSync(path.join(source, "package.json"), "utf8")).name;
    }
    // cron's factory can bootstrap launchd/daemon state. Do not invoke it.
    if (entry.name === "envy-cron" || packageName?.endsWith("/pi-extension-cron") || packageName === "pi-extension-cron") {
      result.status = "skipped";
      continue;
    }
    const isolatedAgent = path.join(root, `agent-${i}`);
    fs.mkdirSync(path.join(isolatedAgent, "extensions"), { recursive: true });
    fs.symlinkSync(source, path.join(isolatedAgent, "extensions", entry.name));
    const loaded = await discoverAndLoadExtensions([], cwd, isolatedAgent);
    result.errorCount = loaded.errors.length;
    result.loadedCount = loaded.extensions.length;
    if (loaded.errors.length === 0 && loaded.extensions.length > 0) result.status = "ok";
    for (const extension of loaded.extensions) {
      result.tools.push(...extension.tools.keys());
      result.commands.push(...extension.commands.keys());
    }
    result.tools = [...new Set(result.tools)].filter(safeRegistration).sort();
    result.commands = [...new Set(result.commands)].filter(safeRegistration).sort();
    loaded.runtime.invalidate();
  }
  output.status = output.extensions.some(entry => !["ok", "skipped"].includes(entry.status) || entry.guard.status === "error") ? "error" : "ok";
} catch {
  output.status = "error";
} finally {
  if (root) fs.rmSync(root, { recursive: true, force: true });
}
write(JSON.stringify(output) + "\n");
// Factories may register timers; no lifecycle event is dispatched by this probe.
process.exit(output.status === "ok" ? 0 : 1);
