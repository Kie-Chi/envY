// Verify the installed Pi artifact without credentials or network requests.
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const modulesDir = resolve(process.argv[2]);
const require = createRequire(`${modulesDir}/envy-pi-check.cjs`);
const load = (pkg, file) => import(pathToFileURL(`${modulesDir}/@earendil-works/${pkg}/dist/${file}.js`));
const { Check } = await import(pathToFileURL(require.resolve("typebox/value")));
const { getSupportedThinkingLevels, clampThinkingLevel } = await load("pi-ai", "models");
const { normalizeContext } = await load("pi-ai", "utils/transcript");
const { isValidThinkingLevel, parseArgs } = await load("pi-coding-agent", "cli/args");
const { ModelsConfigSchema } = await load("pi-coding-agent", "core/model-config");
const { SettingsManager } = await load("pi-coding-agent", "core/settings-manager");
const { ThinkingSelectorComponent } = await load("pi-coding-agent", "modes/interactive/components/thinking-selector");
const themeModule = await load("pi-coding-agent", "modes/interactive/theme/theme");

const ordinary = {
  id: "envy-thinking-check",
  name: "envY thinking check",
  provider: "envy-pi",
  api: "openai-responses",
  baseUrl: "https://example.invalid/v1",
  reasoning: true,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128000,
  maxTokens: 4096,
};
const extended = {
  ...ordinary,
  thinkingLevelMap: { xhigh: "xhigh", max: "max", ultra: "ultra" },
};
const levels = ["off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"];
assert.deepEqual(getSupportedThinkingLevels(ordinary), levels.slice(0, 5));
assert.equal(clampThinkingLevel(ordinary, "ultra"), "high");
assert.deepEqual(getSupportedThinkingLevels(extended), levels);
assert.equal(clampThinkingLevel(extended, "max"), "max");
assert.equal(clampThinkingLevel(extended, "ultra"), "ultra");
assert.deepEqual(getSupportedThinkingLevels({ ...extended, reasoning: false }), ["off"]);
assert.equal(isValidThinkingLevel("ultra"), true);
assert.equal(parseArgs(["--thinking", "ultra"]).thinking, "ultra");

assert.equal(Check(ModelsConfigSchema, {
  providers: {
    "envy-pi": {
      api: ordinary.api,
      baseUrl: ordinary.baseUrl,
      apiKey: "$OPENAI_API_KEY",
      models: [{ id: ordinary.id, reasoning: true, thinkingLevelMap: extended.thinkingLevelMap }],
    },
  },
}), true);

const settings = SettingsManager.inMemory();
settings.setDefaultThinkingLevel("ultra");
await settings.flush();
assert.equal(settings.getDefaultThinkingLevel(), "ultra");
themeModule.initTheme("dark", false);
const selector = new ThinkingSelectorComponent("ultra", levels, () => {}, () => {});
assert.equal(selector.getSelectList().getSelectedItem().value, "ultra");
assert.ok(selector.render(100).join("\n").includes("ultra"));
assert.equal(typeof themeModule.theme.getThinkingBorderColor("ultra")("border"), "string");

// Throw from the payload hook before the SDK sends anything. Block fetch too,
// so a regression in the hook cannot accidentally turn this into an API test.
globalThis.fetch = async () => { throw new Error("Network is forbidden in the Pi install check"); };
const context = normalizeContext({
  messages: [{ role: "user", content: "Test", timestamp: 0 }],
});
for (const api of ["openai-responses", "openai-completions"]) {
  const { streamSimple } = await load("pi-ai", `api/${api}`);
  for (const reasoning of ["xhigh", "max", "ultra"]) {
    let payload;
    await streamSimple({ ...extended, api }, context, {
      apiKey: "envy-install-check-placeholder",
      reasoning,
      maxRetries: 0,
      onPayload(request) {
        payload = request;
        throw new Error("Payload captured; no request sent");
      },
    }).result();
    assert.ok(payload, `${api}: ${reasoning} never reached the payload hook`);
    assert.equal(api === "openai-responses" ? payload.reasoning.effort : payload.reasoning_effort, reasoning);
  }
}

console.log("Pi thinking: CLI, model schema, settings, selector, clamping, and both API payloads passed");
