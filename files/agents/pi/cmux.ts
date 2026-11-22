import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const source = "@cmuxPiSource@";
type Handler = (event: unknown, ctx: ExtensionContext) => unknown;

function interactive(ctx: ExtensionContext): boolean {
  return ctx.mode === "tui" && ctx.hasUI && Boolean(process.stdout.isTTY)
    && Boolean(process.env.CMUX_SURFACE_ID) && process.env.CMUX_PI_HOOKS_DISABLED !== "1";
}

function nativeAlreadyConfigured(): boolean {
  const agentDir = process.env.PI_CODING_AGENT_DIR || path.join(os.homedir(), ".pi/agent");
  const candidates = [
    path.join(agentDir, "extensions/cmux-session.ts"),
    path.join(process.cwd(), ".pi/extensions/cmux-session.ts"),
  ];
  for (let index = 2; index < process.argv.length; index++) {
    const argument = process.argv[index];
    if (argument === "-e" || argument === "--extension") candidates.push(process.argv[++index] || "");
    else if (argument.startsWith("--extension=")) candidates.push(argument.slice(12));
  }
  return candidates.some(candidate => {
    try {
      return fs.readFileSync(candidate, "utf8").startsWith("// cmux-pi-session-extension-marker ");
    } catch {
      return false;
    }
  });
}

export default async function (pi: ExtensionAPI) {
  if (!process.env.CMUX_SURFACE_ID || process.env.CMUX_PI_HOOKS_DISABLED === "1" || nativeAlreadyConfigured()) return;
  try {
    // The installed cmux supplies its own bridge; envY only gates its events.
    fs.accessSync(source, fs.constants.R_OK);
    const { default: register } = await import(pathToFileURL(source).href);
    if (typeof register !== "function") throw new Error("Invalid cmux extension");
    const on = pi.on.bind(pi) as (event: string, handler: Handler) => unknown;
    const guarded = new Proxy(pi, {
      get(target, key, receiver) {
        if (key !== "on") return Reflect.get(target, key, receiver);
        return (event: string, handler: Handler) => on(event, (value, ctx) => {
          if (interactive(ctx)) return handler(value, ctx);
        });
      },
    });
    await register(guarded);
  } catch {
    pi.on("session_start", (_event, ctx) => {
      if (interactive(ctx)) {
        ctx.ui.notify("cmux agent hooks are unavailable. Run envy-cmux-hooks, then restart Pi.", "warning");
      }
    });
  }
}
