// Verify the installed Pi retries transient gateway errors and still stops on
// deterministic ones, without credentials or network requests.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const modulesDir = resolve(process.argv[2]);
const scope = modulesDir + "/@earendil-works/";
const { isRetryableAssistantError } = await import(pathToFileURL(scope + "pi-ai/dist/utils/retry.js"));
const retryable = (errorMessage) => isRetryableAssistantError({ role: "assistant", stopReason: "error", errorMessage });
const bedrockEvent = (type, message) =>
  JSON.stringify({ content_block: null, delta: null, error: { message, type }, index: 0, message: null, type: "error" });

for (const message of [
  bedrockEvent("internalServerException", "An unexpected error has occurred internal to the API."),
  bedrockEvent("InternalServerException", "An unexpected error has occurred internal to the API."),
  bedrockEvent("throttlingException", "Too many tokens."),
  bedrockEvent("serviceUnavailableException", "Service is not available."),
  bedrockEvent("modelStreamErrorException", "Stream failed."),
  bedrockEvent("modelNotReadyException", "Model is not ready."),
  bedrockEvent("modelTimeoutException", "Model took too long."),
  "Unknown error (no error details in response)",
  "Could not parse Anthropic SSE event content_block_delta: Unexpected end of JSON input; data=; raw=event: content_block_delta",
  "This request triggered restrictions on violative cyber content and was blocked under Anthropic's Usage Policy.",
]) {
  assert.equal(retryable(message), true, message);
}

for (const message of [
  bedrockEvent("ValidationException", "There was an issue with the format or content of your request."),
  bedrockEvent("ValidationException", "Output blocked by content filtering policy"),
  'envy-pi API error (403): {"message":"model x is not available for user y","type":"invalid_request_error"}',
  'envy-pi API error (402): {"message":"No budget pool with available quota could be selected.","type":"billing_error"}',
  "This operation was aborted",
]) {
  assert.equal(retryable(message), false, message);
}

// The CLI runs its bundled copy of the retry policy, not the library build.
const chunks = scope + "pi-coding-agent/dist/bundle/chunks/";
const bundled = readdirSync(chunks)
  .map((name) => readFileSync(chunks + name, "utf8"))
  .filter((source) => source.includes("RETRYABLE_PROVIDER_ERROR_PATTERN"));
assert.ok(bundled.length > 0);
for (const source of bundled) {
  assert.ok(source.includes('"InternalServerException"'));
  assert.ok(source.includes('"no error details in response"'));
  assert.ok(source.includes('"violative cyber content"'));
}
console.log("Pi retry pattern check passed");
