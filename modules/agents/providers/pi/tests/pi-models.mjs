// Exercise managed model metadata and the real /btw executor without API calls.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL, fileURLToPath } from "node:url";
import { test, after } from "node:test";

const [nodeModules, fixturesPath, btwPackage] = process.argv.slice(2);
assert.ok(nodeModules && fixturesPath && btwPackage,
  "Usage: node pi-models.mjs <Pi node_modules> <model fixtures.json> <btw package>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-models-"));
process.env.PI_CODING_AGENT_DIR = path.join(root, "agent");
let networkCalls = 0;
const forbidNetwork = async () => {
  networkCalls++;
  throw new Error("Network is forbidden in Pi model regressions");
};
globalThis.fetch = forbidNetwork;
after(() => {
  fs.rmSync(root, { recursive: true, force: true });
  assert.equal(networkCalls, 0, "No provider request may reach fetch");
});

const modulesDir = fs.realpathSync(nodeModules);
const require = createRequire(path.join(modulesDir, "envy-pi-model-check.cjs"));
const load = (pkg, file) => import(pathToFileURL(path.join(modulesDir, "@earendil-works", pkg, "dist", file + ".js")));
const { Check } = await import(pathToFileURL(require.resolve("typebox/value")));
const { ModelsConfigSchema } = await load("pi-coding-agent", "core/model-config");
const { getSupportedThinkingLevels, clampThinkingLevel } = await load("pi-ai", "models");
const { normalizeContext } = await load("pi-ai", "utils/transcript");
const { createJiti } = require("jiti");
const aliases = Object.fromEntries(["pi-ai", "pi-coding-agent", "pi-tui", "pi-agent-core"].map(name => [
  "@earendil-works/" + name, path.join(modulesDir, "@earendil-works", name, "dist/index.js"),
]));
const jiti = createJiti(fileURLToPath(import.meta.url), { alias: aliases, moduleCache: true, fsCache: false });
const { executeBtw } = await jiti.import(path.join(fs.realpathSync(btwPackage), "btw.ts"));
const fixtures = JSON.parse(fs.readFileSync(fixturesPath, "utf8"));
const capturedMessage = "Payload captured; no request sent";
const effort = (api, payload) => api === "openai-responses" ? payload.reasoning?.effort
  : api === "openai-completions" ? payload.reasoning_effort : payload.output_config?.effort;

async function capture(model, context, options = {}) {
  const { streamSimple } = await load("pi-ai", "api/" + model.api);
  let payload;
  const response = await streamSimple(model, normalizeContext(context), {
    ...options,
    apiKey: "envy-offline-placeholder",
    fetch: forbidNetwork,
    maxRetries: 0,
    onPayload(request) {
      payload = request;
      throw new Error(capturedMessage);
    },
  }).result();
  assert.ok(payload, "The real provider encoder must reach the payload hook");
  assert.equal(response.stopReason, "error");
  assert.equal(response.errorMessage, capturedMessage);
  return { payload, response };
}

for (const fixture of fixtures) {
  const provider = fixture.data.providers["envy-pi"];
  const model = {
    ...provider.models[0], api: provider.api, provider: "envy-pi", baseUrl: provider.baseUrl,
    cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  };
  const isSol = !fixture.expectedLevels.includes("minimal");

  test(fixture.name + ": managed schema and supported levels", () => {
    assert.equal(Check(ModelsConfigSchema, fixture.data), true);
    assert.equal(provider.apiKey, model.api === "anthropic-messages" ? "$ANTHROPIC_API_KEY" : "$OPENAI_API_KEY");
    assert.deepEqual(getSupportedThinkingLevels(model), fixture.expectedLevels);
    if (isSol) {
      for (const level of ["off", "minimal", "ultra"]) assert.equal(model.thinkingLevelMap[level], null, level);
    }
  });

  test(fixture.name + ": actual btw default request", async () => {
    let payload;
    let calls = 0;
    const controller = new AbortController();
    const runtime = {
      async completeSimple(requestModel, context, options) {
        assert.equal(this, runtime, "The auth-aware runtime facade must remain bound");
        assert.equal(options.signal, controller.signal);
        assert.equal(options.reasoning, undefined, "Btw must not inherit the primary thinking level");
        assert.equal(options.apiKey, undefined, "Btw must keep runtime auth resolution");
        assert.deepEqual(context.tools, []);
        calls++;
        const captured = await capture(requestModel, context, options);
        payload = captured.payload;
        return { ...captured.response, stopReason: "stop", errorMessage: undefined,
          content: [{ type: "text", text: "Offline side answer" }] };
      },
    };
    const ctx = {
      model,
      getThinkingLevel: () => "high",
      sessionManager: { getSessionFile: () => undefined, getSessionId: () => fixture.name, getBranch: () => [] },
      modelRegistry: { runtime, getApiKeyAndHeaders: async () => ({ ok: true, apiKey: "unused-preflight-placeholder" }) },
    };
    const result = await executeBtw("Offline side question", ctx, controller);
    assert.equal(result.kind, "success", result.error);
    assert.equal(result.answer, "Offline side answer");
    assert.equal(calls, 1);
    assert.ok(payload);
    assert.equal(effort(model.api, payload) ?? null, fixture.defaultEffort);
    assert.equal(payload.tools, undefined, "Btw must not expose tools");
    if (isSol) {
      assert.equal(payload.reasoning, undefined);
      assert.equal(payload.reasoning_effort, undefined);
    } else if (model.api === "anthropic-messages") {
      assert.notEqual(payload.thinking?.type, "disabled");
    }
  });

  const requestedLevels = ["low", "medium", "high"];
  if (isSol) requestedLevels.push("off", "minimal", "xhigh", "max", "ultra");
  for (const reasoning of requestedLevels) {
    test(fixture.name + ": explicit " + reasoning + " uses a legal effort", async () => {
      const { payload } = await capture(model, { messages: [{ role: "user", content: "Offline", timestamp: 0 }] }, { reasoning });
      const expected = fixture.expectedLevels.includes(reasoning) ? reasoning
        : ["off", "minimal"].includes(reasoning) ? "low"
          : fixture.expectedLevels.includes("max") ? "max" : "high";
      assert.equal(clampThinkingLevel(model, reasoning), expected);
      assert.equal(effort(model.api, payload), expected);
    });
  }
}
