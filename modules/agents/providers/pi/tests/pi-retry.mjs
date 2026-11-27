// Verify the installed Pi retries transient gateway errors and still stops on
// deterministic ones, without credentials or network requests.
import assert from "node:assert/strict";
import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

const modulesDir = resolve(process.argv[2]);
const scope = modulesDir + "/@earendil-works/";
const { isRetryableAssistantError, retryAssistantCall } = await import(pathToFileURL(scope + "pi-ai/dist/utils/retry.js"));
const retryable = (errorMessage) => isRetryableAssistantError({ role: "assistant", stopReason: "error", errorMessage });
const bedrockEvent = (type, message) =>
  JSON.stringify({ content_block: null, delta: null, error: { message, type }, index: 0, message: null, type: "error" });
const eofError = "upstream_eof_before_terminal";
const eofEvent = JSON.stringify({ error: { message: eofError, type: "api_error" }, index: 0, type: "error" });
const policy = { enabled: true, maxRetries: 3, baseDelayMs: 0 };

for (const message of [
  eofError,
  eofEvent,
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
  bedrockEvent("api_error", "Request rejected"),
  bedrockEvent("billing_error", eofError),
  bedrockEvent("insufficient_quota", eofError),
]) {
  assert.equal(retryable(message), false, message);
  let calls = 0;
  const failure = { role: "assistant", stopReason: "error", errorMessage: message };
  const result = await retryAssistantCall(async () => { calls++; return failure; }, policy);
  assert.equal(result, failure);
  assert.equal(calls, 1, message);
}

// Exercise the shared retry loop: recover on the next call, or stop after the
// configured budget. All responses are local fixtures and delays are zero.
const failure = { role: "assistant", stopReason: "error", errorMessage: eofEvent };
const success = { role: "assistant", stopReason: "stop" };
for (const failedCalls of [1, Infinity]) {
  let calls = 0;
  const scheduled = [];
  const finished = [];
  const result = await retryAssistantCall(
    async () => ++calls <= failedCalls ? failure : success,
    policy,
    undefined,
    {
      onRetryScheduled: (...args) => { scheduled.push(args); },
      onRetryFinished: (...args) => { finished.push(args); },
    },
  );
  const recovered = failedCalls === 1;
  const attempts = recovered ? 1 : policy.maxRetries;
  assert.equal(result, recovered ? success : failure);
  assert.equal(calls, attempts + 1);
  assert.deepEqual(scheduled, Array.from({ length: attempts }, (_, i) => [i + 1, 3, 0, eofEvent]));
  assert.deepEqual(finished, [recovered ? [true, attempts] : [false, attempts, eofEvent]]);
}

// An explicit opt-out or an aborted response must still prevent retries.
const aborted = { ...failure, stopReason: "aborted" };
assert.equal(isRetryableAssistantError(aborted), false);
for (const [response, retryPolicy] of [
  [failure, { ...policy, enabled: false }],
  [failure, { ...policy, maxRetries: 0 }],
  [aborted, policy],
]) {
  let calls = 0;
  const result = await retryAssistantCall(async () => { calls++; return response; }, retryPolicy);
  assert.equal(result, response);
  assert.equal(calls, 1);
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
  assert.ok(source.includes('"upstream_eof_before_terminal"'));
  assert.ok(source.includes('"violative cyber content"'));
}
console.log("Pi retry pattern check passed");
