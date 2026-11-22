import fs from "node:fs";
import path from "node:path";
import os from "node:os";
import { stripTypeScriptTypes } from "node:module";
import { spawnSync } from "node:child_process";

const [cli, destination, ...agents] = process.argv.slice(2);
if (!cli || !destination || agents.some(agent => !["pi", "codex"].includes(agent))) {
  throw new Error("Usage: setup.mjs <cmux> <Pi bridge path> [pi] [codex]");
}

function run(args) {
  // These commands only generate local configuration. An explicit unused path
  // avoids socket discovery when cmux is closed; no connection is made.
  const localArgs = ["--socket", destination + ".config-only.sock", ...args];
  const result = spawnSync(cli, localArgs, { encoding: "utf8", timeout: 30000, maxBuffer: 4 * 1024 * 1024 });
  if (result.error || result.status !== 0) {
    throw new Error("cmux " + args.join(" ") + " failed" + (result.error ? ": " + result.error.message : ""));
  }
  return result.stdout;
}

if (agents.includes("pi")) {
  const source = run(["hooks", "pi", "extension-source"]);
  if (!source.startsWith("// cmux-pi-session-extension-marker ")) {
    throw new Error("cmux returned an unrecognized Pi bridge; the existing bridge was retained");
  }
  // Remove types without adding dependencies to Pi or changing cmux behavior.
  const javascript = stripTypeScriptTypes(source, { mode: "strip" });
  fs.mkdirSync(path.dirname(destination), { recursive: true, mode: 0o700 });
  const previous = fs.existsSync(destination) ? fs.readFileSync(destination, "utf8") : null;
  if (previous !== javascript) {
    const temporary = destination + "." + process.pid + ".tmp";
    try {
      fs.writeFileSync(temporary, javascript, { flag: "wx", mode: 0o600 });
      fs.renameSync(temporary, destination);
    } finally {
      fs.rmSync(temporary, { force: true });
    }
  }
  console.log("cmux: Pi bridge is ready");
}

if (agents.includes("codex")) {
  // Native installation skips an agent whose config directory does not exist.
  fs.mkdirSync(path.join(os.homedir(), ".codex"), { recursive: true, mode: 0o700 });
  // The native installer appends its handlers and trusts only those handlers.
  // Avoid the launch wrapper, which may bypass hook trust for all handlers.
  console.log(run(["hooks", "codex", "install", "--yes"]).trim());
}
