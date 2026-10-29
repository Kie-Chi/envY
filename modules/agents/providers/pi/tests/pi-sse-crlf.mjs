// Verify the installed Pi parses Anthropic SSE whose CRLF line endings are
// split across network chunks, without credentials or network requests.
import assert from "node:assert/strict";
import { readdirSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const modulesDir = resolve(process.argv[2]);
const importFile = (file) => import(pathToFileURL(modulesDir + "/@earendil-works/" + file));
const { normalizeContext } = await importFile("pi-ai/dist/utils/transcript.js");
// The CLI runs its bundled copy of the provider, not the library build.
const chunks = "pi-coding-agent/dist/bundle/chunks";
const bundled = readdirSync(modulesDir + "/@earendil-works/" + chunks)
  .filter((name) => /^anthropic-messages-.*\.js$/.test(name))
  .map((name) => chunks + "/" + name);
assert.equal(bundled.length, 1);
const providers = ["pi-ai/dist/api/anthropic-messages.js", ...bundled];

const model = {
  id: "envy-sse-check",
  name: "envY SSE check",
  provider: "envy-pi",
  api: "anthropic-messages",
  baseUrl: "https://example.invalid",
  reasoning: false,
  input: ["text"],
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
  contextWindow: 128000,
  maxTokens: 4096,
};
const usage = { input_tokens: 1, output_tokens: 3 };
const words = ["one", " two", " three"];
const events = [
  ["message_start", { type: "message_start", message: { id: "msg_envy", type: "message", role: "assistant", model: model.id, content: [], usage } }],
  ["content_block_start", { type: "content_block_start", index: 0, content_block: { type: "text", text: "" } }],
  ...words.map((text) => ["content_block_delta", { type: "content_block_delta", index: 0, delta: { type: "text_delta", text } }]),
  ["content_block_stop", { type: "content_block_stop", index: 0 }],
  ["message_delta", { type: "message_delta", delta: { stop_reason: "end_turn" }, usage }],
  ["message_stop", { type: "message_stop" }],
];
const body = (eol) => events.map(([name, data]) => "event: " + name + eol + "data: " + JSON.stringify(data) + eol + eol).join("");

// Split after every CR so each CRLF pair straddles two chunks.
const splitAfterCarriageReturns = (text) => text.split(/(?<=\r)/);

async function run(stream, chunks) {
  const encoder = new TextEncoder();
  const fetch = async () => new Response(new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  }), { status: 200, headers: { "content-type": "text/event-stream" } });
  const context = normalizeContext({ messages: [{ role: "user", content: "hi", timestamp: Date.now() }] });
  const message = await stream(model, context, { apiKey: "envy-check", fetch, maxRetries: 0 }).result();
  assert.equal(message.errorMessage, undefined);
  assert.equal(message.stopReason, "stop");
  assert.deepEqual(message.content.map((block) => block.text), [words.join("")]);
}

for (const provider of providers) {
  const { stream } = await importFile(provider);
  await run(stream, [body("\n")]);
  await run(stream, [body("\r\n")]);
  await run(stream, splitAfterCarriageReturns(body("\r\n")));
  // A final CR is still a line end once the stream has finished.
  await run(stream, splitAfterCarriageReturns(body("\r\n").replace(/\n$/, "")));
}
console.log("Pi Anthropic SSE CRLF check passed");
