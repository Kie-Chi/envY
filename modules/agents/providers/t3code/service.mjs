import { spawn } from "node:child_process";
import { chmod, lstat, mkdir, open, readFile, rename, unlink } from "node:fs/promises";
import { dirname, resolve } from "node:path";
import { parseEnv } from "node:util";
import { pathToFileURL } from "node:url";

const assignment = /^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=/u;
const managedKeys = new Set(["T3CODE_HOST", "T3CODE_PORT", "T3CODE_LAN"]);

export function upsertServiceEnv(contents, entries) {
  for (const [key, value] of Object.entries(entries)) {
    if (!managedKeys.has(key) || typeof value !== "string" || !/^[A-Za-z0-9_.:%[\]-]+$/u.test(value)) {
      throw new Error("Invalid T3 Code service declaration");
    }
  }
  const source = contents.split("\n");
  const written = new Set();
  const lines = [];
  for (let index = 0, offset = 0; index < source.length;) {
    const line = source[index];
    const match = assignment.exec(line);
    let end = index + 1;
    if (match !== null) {
      const rest = line.slice(match[0].length).replace(/^[ \t\r]+/u, "");
      const quote = rest[0];
      if ((quote === '"' || quote === "'" || quote === String.fromCharCode(96)) && !rest.includes(quote, 1)) {
        const closing = contents.indexOf(quote, offset + line.length);
        if (closing !== -1) end += contents.slice(offset + line.length, closing).split("\n").length - 1;
      }
    }
    const span = source.slice(index, end);
    offset += span.reduce((length, part) => length + part.length + 1, 0);
    index = end;
    const key = match?.[1];
    if (key === undefined || !Object.hasOwn(entries, key)) {
      lines.push(span.join("\n"));
    } else if (!written.has(key)) {
      lines.push(key + "=" + entries[key]);
      written.add(key);
    }
  }
  while (lines.at(-1) === "") lines.pop();
  for (const [key, value] of Object.entries(entries)) {
    if (!written.has(key)) lines.push(key + "=" + value);
  }
  return lines.join("\n") + "\n";
}

async function snapshot(target) {
  try {
    const stat = await lstat(target);
    if (!stat.isFile()) throw new Error("Service environment must be a regular file");
    return { stat, text: await readFile(target, "utf8") };
  } catch (error) {
    if (error.code === "ENOENT") return { stat: null, text: "" };
    throw error;
  }
}

export async function mergeServiceEnv(target, entries) {
  const before = await snapshot(target);
  const next = upsertServiceEnv(before.text, entries);
  await mkdir(dirname(target), { recursive: true, mode: 0o700 });
  if (before.stat && next === before.text) {
    await chmod(target, 0o600);
    return false;
  }
  const temporary = target + ".envy-" + process.pid;
  const handle = await open(temporary, "wx", 0o600);
  try {
    await handle.writeFile(next);
    await handle.sync();
    await handle.close();
    const current = await snapshot(target);
    if (current.text !== before.text || current.stat?.ino !== before.stat?.ino) {
      throw new Error("Service environment changed during activation");
    }
    await rename(temporary, target);
    return true;
  } finally {
    await handle.close().catch(() => {});
    await unlink(temporary).catch((error) => { if (error.code !== "ENOENT") throw error; });
  }
}

export async function runService(envFile, command, args = ["serve", "--mode", "web", "--no-browser"]) {
  const environment = parseEnv(await readFile(envFile, "utf8"));
  const child = spawn(command, args, {
    stdio: "inherit",
    env: { ...process.env, ...environment, T3CODE_HOME: dirname(envFile), T3CODE_DISABLE_AUTO_UPDATE: "1" },
  });
  const signals = ["SIGTERM", "SIGINT"];
  const handlers = signals.map((signal) => {
    const handler = () => child.kill(signal);
    process.on(signal, handler);
    return handler;
  });
  try {
    return await new Promise((accept, reject) => {
      child.once("error", reject);
      child.once("exit", (code, signal) => accept(code ?? (signal === "SIGTERM" ? 0 : 1)));
    });
  } finally {
    signals.forEach((signal, index) => process.off(signal, handlers[index]));
  }
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) {
  const [operation, target, argument] = process.argv.slice(2);
  try {
    if (operation === "merge") {
      await mergeServiceEnv(target, JSON.parse(await readFile(argument, "utf8")));
    } else if (operation === "run") {
      process.exitCode = await runService(target, argument);
    } else {
      throw new Error("Expected merge or run");
    }
  } catch (error) {
    console.error("T3 Code service operation failed: " + error.message);
    process.exitCode = 1;
  }
}
