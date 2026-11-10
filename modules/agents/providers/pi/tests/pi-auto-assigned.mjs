import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { syncBuiltinESMExports } from "node:module";
import net from "node:net";
import tls from "node:tls";
import http from "node:http";
import https from "node:https";
import dgram from "node:dgram";
import { after, test } from "node:test";

const [packagePath, nodeModules] = process.argv.slice(2);
assert.ok(packagePath && nodeModules, "Usage: node pi-auto-assigned.mjs <extension> <Pi node_modules>");
const root = fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-auto-assigned-"));
const cwd = path.join(root, "project");
const agentDir = path.join(root, "agent");
for (const dir of [cwd, agentDir, path.join(root, "home"), path.join(root, "tmp")]) fs.mkdirSync(dir);
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, { HOME: path.join(root, "home"), TMPDIR: path.join(root, "tmp"),
  PATH: "/usr/bin:/bin", PI_CODING_AGENT_DIR: agentDir, PI_OFFLINE: "1", JITI_FS_CACHE: "false" });
const blocked = () => { throw new Error("Network is forbidden in delegated approval tests"); };
globalThis.fetch = blocked;
net.connect = net.createConnection = net.Socket.prototype.connect = tls.connect = blocked;
http.request = http.get = https.request = https.get = dgram.createSocket = blocked;
syncBuiltinESMExports();
const originalCwd = process.cwd();
process.chdir(cwd);
const host = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-coding-agent/dist/index.js")));
const ai = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-ai/dist/index.js")));
const tui = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-tui/dist/index.js")));
const { KeybindingsManager } = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-coding-agent/dist/core/keybindings.js")));
const keybindings = KeybindingsManager.create(agentDir);
tui.setKeybindings(keybindings);
host.initTheme("dark", false);
const themes = await import(pathToFileURL(path.join(nodeModules, "@earendil-works/pi-coding-agent/dist/modes/interactive/theme/theme.js")));
const sessions = [];
after(async () => {
  for (const session of sessions) {
    await session.extensionRunner.emit({ type: "session_shutdown", reason: "exit" });
    session.dispose();
  }
  process.chdir(originalCwd);
  fs.rmSync(root, { recursive: true, force: true });
});

const usage = { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };
async function fixture({ main = [], verdicts = [], source = packagePath, ui, codemode = false } = {}) {
  let mainCalls = 0;
  let reviewCalls = 0;
  const requests = [];
  const mainRequests = [];
  const stream = (model, context, options) => {
    const review = JSON.stringify(context.messages.filter(message => message.role === "system")).includes("independent task reviewer");
    if (review) {
      reviewCalls++;
      assert.equal(ai.getCurrentTools(context.messages).length, 0, "the reviewer must have no tools");
      requests.push(JSON.parse(context.messages.findLast(m => m.role === "user").content[0].text));
    } else { mainCalls++; mainRequests.push(context.messages); }
    const next = (review ? verdicts : main).shift();
    assert.notEqual(next, undefined, "Unexpected " + (review ? "review" : "main") + " request");
    const output = ai.createAssistantMessageEventStream();
    if (typeof next === "function") return next(output, model, options);
    const content = Array.isArray(next) ? next : [{ type: "text", text: typeof next === "object" ? JSON.stringify(next) : next }];
    const message = { role: "assistant", api: model.api, provider: model.provider, model: model.id,
      timestamp: Date.now(), stopReason: content.some(c => c.type === "toolCall") ? "toolUse" : "stop", usage, content };
    output.push({ type: "start", partial: message });
    output.push({ type: "done", reason: message.stopReason, message });
    output.end();
    return output;
  };
  const loader = new host.DefaultResourceLoader({ cwd, agentDir, additionalExtensionPaths: [source],
    extensionFactories: [...(codemode ? [{ name: "codemode", builtin: true, replaceable: true, factory: host.createCodemodeExtension() }] : []), pi => pi.registerProvider("envy-offline", {
      api: "envy-offline-api", baseUrl: "http://invalid.test", apiKey: "offline-test-only", streamSimple: stream,
      models: [{ id: "test", name: "test", reasoning: false, input: ["text"], contextWindow: 200000, maxTokens: 4096,
        cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    })],
  });
  await loader.reload();
  assert.deepEqual(loader.getExtensions().errors, []);
  const extensions = loader.getExtensions().extensions;
  const extension = extensions.find(e => e.commands.has("auto-assigned"));
  assert.ok(extension);
  const tools = extensions.flatMap(e => [...e.tools.keys()]);
  assert.equal(tools.filter(name => name === "questionnaire").length, 1);
  const runtime = await host.ModelRuntime.create({ authPath: path.join(agentDir, "auth.json"), modelsPath: path.join(agentDir, "models.json") });
  const { session } = await host.createAgentSession({ cwd, agentDir, resourceLoader: loader, modelRuntime: runtime,
    settingsManager: host.SettingsManager.inMemory({ retry: { enabled: false }, compaction: { enabled: false }, ...(codemode ? { defaultTools: ["+codemode"], codemode: { mode: "only" } } : {}) }),
    sessionManager: host.SessionManager.inMemory(cwd),
  });
  sessions.push(session);
  await session.bindExtensions(ui ? { mode: "tui", uiContext: ui } : {});
  await session.setModel(new host.ModelRegistry(runtime).find("envy-offline", "test"));
  const notices = [];
  const context = Object.create(session.extensionRunner.createCommandContext());
  Object.defineProperties(context, {
    mode: { value: "tui" }, hasUI: { value: true },
    ui: { value: { setStatus() {}, notify: text => notices.push(text) } },
  });
  const command = args => extension.commands.get("auto-assigned").handler(args, context);
  return { session, command, autoAssignedCommand: extension.commands.get("auto-assigned"), notices, requests, mainRequests, questionnaire: extension.tools.get("questionnaire").definition, counts: () => ({ main: mainCalls, review: reviewCalls }),
    decisions: () => session.sessionManager.getBranch().filter(e => e.type === "custom" && e.customType === "envy-auto-assigned-review"),
  };
}
const renderQuestionnaire = (h, result) => h.questionnaire.renderResult(result, { expanded: true, isPartial: false }, themes.theme, {}).render(200).join("\n");
const approve = { decision: "continue", reason: "The concrete plan is within the explicitly delegated task." };
const done = { decision: "done", reason: "The requested change and checks are complete." };

test("argument completion offers described actions and filters only the first argument", async () => {
  const h = await fixture();
  const complete = h.autoAssignedCommand.getArgumentCompletions;
  assert.equal(typeof complete, "function");
  const items = await complete("");
  assert.deepEqual(items.map(item => item.value), ["on", "off", "status"]);
  for (const item of items) {
    assert.equal(item.label, item.value);
    assert.ok(item.description?.trim(), "Every action needs a usage description");
  }
  for (const [prefix, expected] of [["o", ["on", "off"]], ["on", ["on"]], ["of", ["off"]], ["s", ["status"]], ["  o", ["on", "off"]], ["  ", ["on", "off", "status"]]]) {
    assert.deepEqual((await complete(prefix)).map(item => item.value), expected, prefix);
  }
  for (const prefix of ["unknown", "ON", "on ", "on Fix the function", "on 修复补全", "off ", "status ", "  on  task"]) {
    assert.equal(await complete(prefix), null, prefix);
  }
  assert.deepEqual(h.counts(), { main: 0, review: 0 });
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("real editor shows action hints for Tab selection and leaves task scope unchanged", { timeout: 5000 }, async () => {
  const h = await fixture();
  const provider = new tui.CombinedAutocompleteProvider([h.autoAssignedCommand], cwd, null);
  const terminal = { requestRender() {}, terminal: { rows: 40, columns: 120 } };
  const editor = new host.CustomEditor(terminal, themes.getEditorTheme(), keybindings);
  editor.setAutocompleteProvider(provider);
  let submitted = false;
  editor.onSubmit = () => { submitted = true; };
  const input = async text => {
    for (const char of text) editor.handleInput(char);
    await new Promise(resolve => setImmediate(resolve));
  };
  for (const [prefix, action] of [["", "on"], ["of", "off"], ["s", "status"]]) {
    editor.setText("/auto-assigne");
    await input("d");
    await input(" " + prefix);
    assert.equal(editor.isShowingAutocomplete(), true);
    const view = editor.render(120).join("\n");
    const items = await h.autoAssignedCommand.getArgumentCompletions(prefix);
    for (const item of items) {
      assert.ok(view.includes(item.label), "Tab must display " + item.label);
      assert.ok(view.includes(item.description), "Tab must display the action hint");
    }
    editor.handleInput("\t");
    assert.equal(editor.getText(), "/auto-assigned " + action);
  }
  const scoped = "/auto-assigned on 修复补全";
  editor.setText("/auto-assigned on ");
  await input("修复补全");
  assert.equal(editor.isShowingAutocomplete(), false);
  await input("\t");
  assert.equal(editor.getText(), scoped);
  assert.equal(submitted, false, "Tab must not execute the command");
  assert.deepEqual(h.counts(), { main: 0, review: 0 });
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("default off: ordinary answers never trigger reviewer calls", async () => {
  const h = await fixture({ main: ["A plan, waiting for your approval."] });
  await h.session.prompt("Implement the small change.");
  assert.deepEqual(h.counts(), { main: 1, review: 0 });
  await h.command("");
  assert.match(h.notices.at(-1), /is off/);
});

test("ordinary plan approval continues the real host and completed work settles", async () => {
  const h = await fixture({ main: ["Plan: modify the requested function and run its check. Waiting for approval.", "The requested change and check are complete."], verdicts: [approve, done] });
  await h.command("on Modify the requested function and run its check.");
  await h.session.prompt("Implement the scoped change.");
  assert.deepEqual(h.counts(), { main: 2, review: 2 }, JSON.stringify({ notices: h.notices, entries: h.session.sessionManager.getBranch().filter(e => e.type === "custom") }));
  assert.deepEqual(h.decisions().map(e => e.data.decision), ["continue", "done"]);
  assert.ok(h.session.sessionManager.getBranch().some(e => e.type === "custom_message" && /Delegated model review/.test(e.content)));
  assert.equal(h.requests[0].scope, "Modify the requested function and run its check.");
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("questionnaire answers go through the same tool-free reviewer", async () => {
  const h = await fixture({ main: [[{ type: "toolCall", id: "question", name: "questionnaire", arguments: { questions: [{ id: "plan", prompt: "Choose the local implementation.", options: [{ value: "small", label: "Small change" }, { value: "large", label: "Large refactor" }] }] } }], "The small implementation is complete."],
    verdicts: [{ ...approve, answers: [{ id: "plan", value: "small" }] }, done] });
  await h.command("on Make the smallest local implementation.");
  await h.session.prompt("Implement the scoped change.");
  const result = h.session.messages.find(m => m.role === "toolResult" && m.toolName === "questionnaire");
  assert.equal(result.isError, false);
  assert.equal(result.details.delegated, true);
  assert.equal(result.details.answers[0].value, "small");
  assert.match(result.content[0].text, /not a user response/);
  assert.match(renderQuestionnaire(h, result), /Delegated model review \(envy-offline\/test\): continue/);
  assert.deepEqual(h.counts(), { main: 2, review: 2 });
});

test("questionnaire corrections reach the assistant and continue without a human dialog", async () => {
  let dialogs = 0;
  const correction = "Include the focused verification before reporting completion.";
  const h = await fixture({ ui: { setStatus() {}, notify() {}, custom: async () => { dialogs++; throw new Error("A correction opened a human dialog"); } },
    main: [[{ type: "toolCall", id: "revise-question", name: "questionnaire", arguments: { questions: [{ id: "plan", prompt: "Approve the local plan without verification?", options: [{ value: "yes", label: "Approve" }] }] } }],
      "Corrected plan: implement the change and run its focused verification.", "The requested change and focused verification are complete."],
    verdicts: [{ decision: "revise", reason: correction }, approve, done],
  });
  await h.command("on Make and verify the requested local change.");
  await h.session.prompt("Continue.");
  const result = h.session.messages.find(m => m.role === "toolResult" && m.toolName === "questionnaire");
  assert.equal(result.isError, false);
  assert.equal(result.details.decision, "revise");
  assert.equal(result.details.delegated, true);
  assert.deepEqual(result.details.answers, []);
  assert.ok(result.content[0].text.includes(correction));
  assert.ok(renderQuestionnaire(h, result).includes(correction));
  assert.match(renderQuestionnaire(h, result), /Delegated model review/);
  assert.ok(h.mainRequests[1].some(m => m.role === "toolResult" && m.toolName === "questionnaire" && m.content.some(c => c.type === "text" && c.text.includes(correction))));
  assert.match(result.content[0].text, /not a user response/);
  assert.equal(dialogs, 0);
  assert.deepEqual(h.decisions().map(e => e.data.decision), ["revise", "continue", "done"]);
  assert.deepEqual(h.counts(), { main: 3, review: 3 });
  assert.ok(h.requests[1].transcript.some(m => m.toolName === "questionnaire" && m.text.includes(correction)));
});

test("questionnaire completion stops delegation and emits a final report without a human dialog", async () => {
  let dialogs = 0;
  const h = await fixture({ ui: { setStatus() {}, notify() {}, custom: async () => { dialogs++; throw new Error("Completion opened a human dialog"); } },
    main: [[{ type: "toolCall", id: "done-question", name: "questionnaire", arguments: { questions: [{ id: "extra", prompt: "The scoped change is verified. Should I do extra work?", options: [{ value: "yes", label: "Do more" }] }] } }],
      "The requested change and verification are complete."], verdicts: [done],
  });
  await h.command("on Make only the requested local change and verify it.");
  await h.session.prompt("Continue.");
  const result = h.session.messages.find(m => m.role === "toolResult" && m.toolName === "questionnaire");
  assert.equal(result.isError, false);
  assert.equal(result.details.decision, "done");
  assert.deepEqual(result.details.answers, []);
  assert.match(result.content[0].text, /Give the final completion report/);
  assert.match(renderQuestionnaire(h, result), /Delegated model review/);
  assert.ok(renderQuestionnaire(h, result).includes(done.reason));
  assert.ok(h.mainRequests[1].some(m => m.role === "toolResult" && m.toolName === "questionnaire" && m.content.some(c => c.type === "text" && /Give the final completion report/.test(c.text))));
  assert.match(result.content[0].text, /not a user response/);
  assert.equal(dialogs, 0);
  assert.deepEqual(h.counts(), { main: 2, review: 1 });
  assert.equal(h.session.messages.at(-1).role, "assistant");
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("questionnaire ask preserves the real human fallback and stops delegation", async () => {
  let dialogs = 0;
  const h = await fixture({ ui: { setStatus() {}, notify() {}, custom: async () => {
    dialogs++;
    return { questions: [], answers: [{ id: "preference", value: "local", label: "Keep local" }], cancelled: false };
  } }, main: [[{ type: "toolCall", id: "human-preference", name: "questionnaire", arguments: { questions: [{ id: "preference", prompt: "Choose your deployment preference.", options: [{ value: "local", label: "Keep local" }] }] } }],
    "The user's preference is recorded; no deployment will run."],
    verdicts: [{ decision: "ask", reason: "The user must supply their deployment preference." }],
  });
  await h.command("on Make a local change; ask the user for any missing preferences.");
  await h.session.prompt("Continue.");
  const result = h.session.messages.find(m => m.role === "toolResult" && m.toolName === "questionnaire");
  assert.equal(dialogs, 1);
  assert.notEqual(result.details.delegated, true);
  assert.equal(result.details.answers[0].value, "local");
  assert.deepEqual(h.counts(), { main: 2, review: 1 });
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("missing user information and out-of-scope work stop automatic continuation", async () => {
  const h = await fixture({ main: ["I need your preferred production account."], verdicts: [{ decision: "ask", reason: "The user must supply their account preference." }] });
  await h.command("on Make a local change without deployment.");
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 1, review: 1 });
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("repeating the same plan stops without a second approval", async () => {
  const h = await fixture({ main: ["Plan: make the local change.", "Plan: make the local change."], verdicts: [approve] });
  await h.command("on Make the local change.");
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 2, review: 1 });
});

test("different replies without implementation progress stop after three continuations", async () => {
  const h = await fixture({ main: ["Plan one.", "Plan two.", "Plan three.", "Still planning."], verdicts: [approve, approve, approve] });
  await h.command("on Implement a local change.");
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 4, review: 3 });
});

test("a correction is delivered and reviewed again before normal completion", async () => {
  const h = await fixture({ main: ["Plan without verification.", "Plan includes the focused check.", "The change is complete."],
    verdicts: [{ decision: "revise", reason: "Include the focused verification before reporting completion." }, approve, done] });
  await h.command("on Change the function and verify it.");
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 3, review: 3 });
});

test("invalid model output fails closed and never continues", async () => {
  const h = await fixture({ main: ["Plan: change a local file."], verdicts: ["not JSON"] });
  await h.command("on Change a local file.");
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 1, review: 1 });
  assert.equal(h.decisions().length, 0);
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("invalid question values never become delegated user choices", async () => {
  const h = await fixture({ main: [[{ type: "toolCall", id: "invalid-answer", name: "questionnaire", arguments: { questions: [{ id: "q", prompt: "Which implementation?", options: [{ value: "small", label: "Small" }] }] } }], "A human response is needed."],
    verdicts: [{ ...approve, answers: [{ id: "q", value: "invented" }] }] });
  await h.command("on Make a small local change.");
  await h.session.prompt("Continue.");
  const result = h.session.messages.find(m => m.role === "toolResult" && m.toolName === "questionnaire");
  assert.notEqual(result.details?.delegated, true);
  assert.deepEqual(h.counts(), { main: 2, review: 1 });
});

test("user cancellation during review aborts it and prevents continuation", { timeout: 5000 }, async () => {
  let started;
  const reviewing = new Promise(resolve => { started = resolve; });
  const h = await fixture({ main: ["Plan awaiting approval."], verdicts: [(output, model, options) => {
    started();
    options.signal.addEventListener("abort", () => output.end(), { once: true });
    return output;
  }] });
  await h.command("on Make a local change.");
  const run = h.session.prompt("Continue.");
  await reviewing;
  await h.session.abort();
  await run;
  assert.deepEqual(h.counts(), { main: 1, review: 1 });
  assert.equal(h.decisions().length, 0);
});

test("review timeout stops even when a provider ignores its abort signal", { timeout: 5000 }, async () => {
  const timeoutPackage = path.join(root, "timeout-extension");
  fs.cpSync(packagePath, timeoutPackage, { recursive: true });
  const entry = path.join(timeoutPackage, "index.ts");
  const source = fs.readFileSync(entry, "utf8");
  assert.equal(source.split("const REVIEW_TIMEOUT_MS = 20_000;").length, 2);
  fs.chmodSync(entry, 0o600);
  fs.writeFileSync(entry, source.replace("const REVIEW_TIMEOUT_MS = 20_000;", "const REVIEW_TIMEOUT_MS = 30;"));
  const h = await fixture({ source: timeoutPackage, main: ["Plan awaiting approval."], verdicts: [output => output] });
  await h.command("on Make a local change.");
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 1, review: 1 });
  await h.command("status");
  assert.match(h.notices.at(-1), /is off/);
});

test("tool cancellation during questionnaire review never opens the human UI", { timeout: 5000 }, async () => {
  let started;
  const reviewing = new Promise(resolve => { started = resolve; });
  let dialogs = 0;
  const h = await fixture({ ui: { setStatus() {}, notify() {}, custom: async () => { dialogs++; throw new Error("Cancelled tool opened a dialog"); } },
    main: [[{ type: "toolCall", id: "cancel-question", name: "questionnaire", arguments: { questions: [{ id: "q", prompt: "Which implementation?", options: [{ value: "small", label: "Small" }] }] } }]],
    verdicts: [output => { started(); return output; }],
  });
  await h.command("on Make a small local change.");
  const run = h.session.prompt("Continue.");
  await reviewing;
  await h.session.abort();
  await run;
  assert.equal(dialogs, 0);
  const result = h.session.messages.find(m => m.role === "toolResult" && m.toolName === "questionnaire");
  assert.equal(result.details.cancelled, true);
  assert.equal(h.decisions().length, 0);
});

test("successful writes count as progress and their evidence reaches the reviewer", async () => {
  const write = index => [{ type: "toolCall", id: "write-" + index, name: "write", arguments: { path: "progress.txt", content: "Step " + index } }];
  const h = await fixture({ main: ["Plan one.", write(1), "Plan two.", write(2), "Plan three.", write(3), "Complete."], verdicts: [approve, approve, approve, done] });
  await h.command("on Implement and verify the requested local steps.");
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 7, review: 4 });
  assert.ok(h.requests[1].transcript.some(m => m.role === "toolResult" && m.toolName === "write" && !m.isError));
  assert.ok(h.requests[1].transcript.some(m => m.role === "delegated-review"));
});

test("codemode-only nested writes count as real progress through the locked host pipeline", async () => {
  const write = index => [{ type: "toolCall", id: "codemode-write-" + index, name: "codemode", arguments: {
    code: "return await tools.write({path: 'codemode-progress.txt', content: 'Step " + index + "'});",
  } }];
  const h = await fixture({ codemode: true, main: ["Code plan one.", write(1), "Code plan two.", write(2), "Code plan three.", write(3), "Complete."],
    verdicts: [approve, approve, approve, done] });
  await h.command("on Implement and verify the requested local steps through codemode.");
  await h.session.prompt("Continue.");
  assert.deepEqual(ai.getCurrentTools(h.mainRequests[0]).map(tool => tool.name), ["codemode"]);
  assert.deepEqual(h.counts(), { main: 7, review: 4 });
  assert.equal(fs.readFileSync(path.join(cwd, "codemode-progress.txt"), "utf8"), "Step 3");
});

test("human questionnaire answers remain in the later review evidence", async () => {
  const h = await fixture({ ui: { setStatus() {}, notify() {}, custom: async () => ({ questions: [], answers: [{ id: "deploy", value: "no", label: "Never deploy" }], cancelled: false }) },
    main: [[{ type: "toolCall", id: "human-question", name: "questionnaire", arguments: { questions: [{ id: "deploy", prompt: "Deploy?", options: [{ value: "no", label: "Never deploy" }] }] } }], "I will keep changes local.", "Plan: keep changes local."],
    verdicts: [{ decision: "ask", reason: "Deployment remains forbidden by the user's answer." }],
  });
  await h.session.prompt("Make a local change; first ask about deployment.");
  await h.command("on Make only a local change.");
  await h.session.prompt("Continue the same task.");
  assert.ok(h.requests[0].transcript.some(m => m.toolName === "questionnaire" && /Never deploy/.test(m.text)));
});

test("off and session reload revoke delegation", async () => {
  const h = await fixture({ main: ["Plan awaiting approval.", "Another plan awaiting approval."] });
  await h.command("on Make a local change.");
  await h.command("off");
  await h.session.prompt("Continue.");
  await h.command("on Make a local change.");
  await h.session.extensionRunner.emit({ type: "session_start" });
  await h.session.prompt("Continue.");
  assert.deepEqual(h.counts(), { main: 2, review: 0 });
});
