import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout } from "node:timers/promises";
import { test } from "node:test";
import { parseEnv } from "node:util";
const helperUrl = process.env.T3CODE_SERVICE_HELPER ?? new URL("../service.mjs", import.meta.url).href;
const { mergeServiceEnv, upsertServiceEnv } = await import(helperUrl);

const declaration = { T3CODE_HOST: "::", T3CODE_PORT: "3773", T3CODE_LAN: "true" };
async function directory(t) {
  const path = await mkdtemp(join(tmpdir(), "envy-t3-service-"));
  t.after(() => rm(path, { recursive: true, force: true }));
  return path;
}

test("preserves multiline values and comments while replacing duplicate managed assignments", () => {
  const original = '# local note\nCUSTOM="first\nT3CODE_HOST=inside\nlast"\nT3CODE_HOST=127.0.0.1\nexport T3CODE_HOST=old\nEXTRA=retained\n';
  const next = upsertServiceEnv(original, declaration);
  const parsed = parseEnv(next);
  assert.equal(parsed.CUSTOM, "first\nT3CODE_HOST=inside\nlast");
  assert.equal(parsed.EXTRA, "retained");
  assert.ok(next.startsWith("# local note\n"));
  for (const [key, value] of Object.entries(declaration)) assert.equal(parsed[key], value);
  assert.equal(next.match(/^T3CODE_HOST=/gm).length, 2); // One is inside the preserved value.
  assert.equal(upsertServiceEnv(next, declaration), next);
});

test("writes a private regular file and does not replace it on repeated activation", async (t) => {
  const root = await directory(t);
  const file = join(root, "service.env");
  await writeFile(file, "OTHER=keep\n", { mode: 0o644 });
  assert.equal(await mergeServiceEnv(file, declaration), true);
  assert.equal((await stat(file)).mode & 0o777, 0o600);
  const first = await stat(file);
  assert.equal(await mergeServiceEnv(file, declaration), false);
  assert.equal((await stat(file)).ino, first.ino);
  assert.equal(parseEnv(await readFile(file, "utf8")).OTHER, "keep");
});

test("refuses symlinks and cannot take ownership of credential fields", async (t) => {
  const root = await directory(t);
  const real = join(root, "unmanaged");
  const link = join(root, "service.env");
  await writeFile(real, "OTHER=keep\n");
  await symlink(real, link);
  await assert.rejects(mergeServiceEnv(link, declaration), /regular file/);
  assert.equal(await readFile(real, "utf8"), "OTHER=keep\n");
  assert.throws(() => upsertServiceEnv("", { API_KEY: "fixture" }), /Invalid/);
  assert.throws(() => upsertServiceEnv("", { T3CODE_HOST: "::\nINJECTED=value" }), /Invalid/);
});

test("loads dotenv as data, sets the original T3 home, and forwards shutdown to the server", async (t) => {
  const root = await directory(t);
  const envFile = join(root, "service.env");
  const result = join(root, "result.json");
  const marker = join(root, "must-not-exist");
  await writeFile(envFile, 'T3CODE_HOST=::\nOPAQUE="$(touch ' + marker + ')"\n');
  const fixture = join(root, "fixture.mjs");
  await writeFile(fixture, 'import {createServer} from "node:http"; import {writeFileSync} from "node:fs"; const server=createServer((q,r)=>r.end("ready")); server.listen(0,"127.0.0.1",()=>writeFileSync(' + JSON.stringify(result) + ',JSON.stringify({pid:process.pid,host:process.env.T3CODE_HOST,home:process.env.T3CODE_HOME,opaque:process.env.OPAQUE,port:server.address().port}))); process.on("SIGTERM",()=>server.close(()=>process.exit(0)));');
  const driver = join(root, "driver.mjs");
  await writeFile(driver, 'import {runService} from ' + JSON.stringify(helperUrl) + '; process.exitCode=await runService(' + JSON.stringify(envFile) + ',process.execPath,[' + JSON.stringify(fixture) + ']);');
  const child = spawn(process.execPath, [driver], { stdio: "ignore" });
  t.after(() => { if (child.exitCode === null) child.kill("SIGTERM"); });
  let data;
  for (let attempt = 0; attempt < 100; attempt++) {
    try { data = JSON.parse(await readFile(result, "utf8")); break; }
    catch (error) { if (error.code !== "ENOENT") throw error; await setTimeout(25); }
  }
  assert.ok(data, "fixture server did not start");
  assert.equal(data.host, "::");
  assert.equal(data.home, root);
  assert.ok(data.opaque.startsWith("$(touch "));
  assert.equal((await fetch("http://127.0.0.1:" + data.port)).status, 200);
  await assert.rejects(stat(marker), { code: "ENOENT" });
  const stopped = once(child, "exit");
  child.kill("SIGTERM");
  const [code] = await stopped;
  assert.equal(code, 0);
  assert.throws(() => process.kill(data.pid, 0), { code: "ESRCH" });
});
