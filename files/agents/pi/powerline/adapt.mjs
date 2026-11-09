import assert from "node:assert/strict";
import fs from "node:fs";

const manifest = JSON.parse(fs.readFileSync("package.json", "utf8"));
assert.equal(manifest.version, "0.11.2");
assert.equal(Object.keys(manifest.dependencies ?? {}).length, 0);
assert.deepEqual(manifest.pi.extensions, ["./extensions"]);
manifest.pi.extensions = ["./envy-footer.ts"];
fs.writeFileSync("package.json", JSON.stringify(manifest, null, 2) + "\n");

let source = fs.readFileSync("extensions/footer.ts", "utf8");
function replace(before, after) {
  assert.equal(source.split(before).length - 1, 1, "Upstream footer changed: " + before);
  source = source.replace(before, after);
}
replace("import { hasNerdFonts, hexFg, withIcon }", "import { hasNerdFonts, withIcon }");
replace("import { readPowerlineSettings }", "import { readPowerlineSettings, readSettings }");
const compact = source.match(/function readAutoCompactEnabled\(cwd: string\): boolean \{[\s\S]*?\n\}/g);
assert.equal(compact?.length, 1);
replace(compact[0],   "function readAutoCompactEnabled(cwd: string): boolean {\n" +
  "  const compaction = readSettings(cwd).compaction as { enabled?: boolean } | undefined;\n" +
  "  return compaction?.enabled !== false;\n}");
replace("return (tui: any, theme: any, footerData: any) =>", "return (tui: any, _initialTheme: any, footerData: any) =>");
replace("render(width: number): string[] {", "render(width: number): string[] {\n        const theme = ctx.ui.theme;");
replace("hexFg('#5faf5f', withIcon(ICON_GIT, branch))", "theme.fg('success', withIcon(ICON_GIT, branch))");
replace("for (const e of ctx.sessionManager.getEntries()) {", 
  "for (const e of ctx.sessionManager.getEntries()) {\n" +
  "          if (e.type === 'custom' && e.customType === 'envy-auto-assigned-review') {\n" +
  "            const usage = (e.data as { usage?: SessionAssistantUsage })?.usage;\n" +
  "            if (usage) { totalInput += usage.input; totalOutput += usage.output; totalCacheRead += usage.cacheRead; totalCacheWrite += usage.cacheWrite; totalCost += usage.cost?.total ?? 0; }\n" +
  "          }");
replace("const statsParts: string[] = [];", "const statsParts: string[] = [ctx.model?.id ?? 'no model'];");
replace("function enable(ctx: ExtensionContext) {\n    enabled = true;", "function enable(ctx: ExtensionContext) {\n    if (!ctx.hasUI) return;\n    enabled = true;");
replace("pi.on('session_start', (_event, ctx) => {", "pi.on('session_start', (_event, ctx) => {\n    isStreaming = false;\n    liveAssistantUsage = null;");
replace("  // ── real-time token updates during streaming ──",   "  pi.on('session_shutdown', (_event, ctx) => {\n" +
  "    if (enabled) disable(ctx);\n" +
  "  });\n\n" +
  "  pi.on('message_start', (event) => {\n" +
  "    if (event.message.role === 'assistant') { isStreaming = true; liveAssistantUsage = null; }\n" +
  "  });\n\n" +
  "  // ── real-time token updates during streaming ──");
fs.writeFileSync("extensions/footer.ts", source);
