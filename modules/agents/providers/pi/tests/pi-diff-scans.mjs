import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { syncBuiltinESMExports } from "node:module";
import { pathToFileURL } from "node:url";

const [diffPackage, nodeModules] = process.argv.slice(2);
assert.ok(diffPackage && nodeModules, "Usage: node pi-diff-scans.mjs <diffPackage> <Pi node_modules>");
const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), "envy-pi-diff-scans-")));
const originalCwd = process.cwd();
const originalEnv = { ...process.env };
const home = path.join(root, "home");
fs.mkdirSync(home);
fs.mkdirSync(path.join(root, "tmp"));
for (const key of Object.keys(process.env)) delete process.env[key];
Object.assign(process.env, {
  HOME: home, TMPDIR: path.join(root, "tmp"), PATH: originalEnv.PATH,
  PI_OFFLINE: "1", PI_CODING_AGENT_DIR: path.join(root, "agent"),
  GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null",
  GIT_TERMINAL_PROMPT: "0", GIT_CEILING_DIRECTORIES: root,
});
// Only the Tracker dependency graph is imported: no SDK, extensions, or sessions.
const { Tracker } = await import(pathToFileURL(path.join(diffPackage, "src/tracker.ts")));
const { SnapshotStore } = await import(pathToFileURL(path.join(diffPackage, "src/snapshot.ts")));
const { Git } = await import(pathToFileURL(path.join(diffPackage, "src/git.ts")));
const { SCAN_LIMITS } = await import(pathToFileURL(path.join(diffPackage, "src/scan-budget.ts")));
const { collectTurnDiffs } = await import(pathToFileURL(path.join(diffPackage, "src/store.ts")));
let sequence = 0;
let failures = 0;
const tests = [];
const test = (name, run) => tests.push({ name, run });
function fixture(name) {
  const cwd = path.join(root, name);
  fs.mkdirSync(cwd, { recursive: true });
  process.chdir(cwd);
  return cwd;
}
function tracker(entries = []) {
  return new Tracker({ appendEntry(customType, data) { entries.push({ type: "custom", customType, data }); } }, new SnapshotStore(path.join(root, `cache-${sequence++}`)));
}
function git(...args) {
  return execFileSync("git", args, { cwd: process.cwd(), stdio: ["ignore", "pipe", "pipe"] });
}
function commit() {
  git("add", ".");
  git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Fixture");
}
const patches = t => t.turns().at(-1).patches;
function exact(t, file, before, after) {
  const p = patches(t).find(p => p.file === file);
  assert.ok(p, `missing patch for ${file}`);
  assert.equal(p.oldHash, SnapshotStore.hash(Buffer.from(before)));
  assert.equal(p.newHash, SnapshotStore.hash(Buffer.from(after)));
  assert.ok(!p.baselineUnknown && !p.unavailable, "explicit old and new sides must remain available");
}
function limited(t) {
  assert.equal(typeof t.coverageLimited, "function", "Tracker must expose coverageLimited()");
  assert.equal(t.coverageLimited(), true);
}
function manyFiles(count = SCAN_LIMITS.entries + 2050) {
  fs.mkdirSync("bulk");
  for (let i = 0; i < count; i++) fs.writeFileSync(path.join("bulk", `${String(i).padStart(5, "0")}.txt`), "");
  return count;
}
async function bounded(operation) {
  let timer;
  try {
    await Promise.race([operation, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("scan did not return while filesystem I/O was suspended")), Math.max(2500, SCAN_LIMITS.milliseconds * 5));
    })]);
  } finally { clearTimeout(timer); }
}

test("ordinary turn start and empty finalize never prepare a workspace baseline", async () => {
  fixture("idle");
  const t = tracker();
  for (const method of ["makeMarker", "walk", "gitWorkspaceSignature", "workspaceStatus", "isGit"]) {
    t[method] = async () => assert.fail(`idle turn called ${method}`);
  }
  await t.onTurnStart();
  await t.finalizeTurn();
  assert.deepEqual(patches(t), []);
});

test("synthetic home and filesystem root forbid tree scans, preserving explicit edits", async () => {
  const available = Git.isAvailable;
  try {
    Git.isAvailable = async () => false;
    for (const cwd of [home, path.parse(root).root]) {
      process.chdir(cwd);
      const t = tracker();
      for (const method of ["makeMarker", "walk", "scanWorkspace"]) {
        t[method] = async () => assert.fail(`protected directory called ${method}`);
      }
      await t.onTurnStart();
      if (cwd === home) {
        manyFiles();
        fs.writeFileSync("target.txt", "old\n");
        await t.recordFileChange("target.txt", { fromToolEvent: true });
        fs.writeFileSync("target.txt", "new\n");
      }
      await t.onBashStart("shell");
      await t.onBashEnd("shell");
      await t.finalizeTurn();
      limited(t);
      if (cwd === home) exact(t, "target.txt", "old\n", "new\n");
    }
  } finally { Git.isAvailable = available; }
});

test("small non-Git shell still discovers newly created files", async () => {
  fixture("small");
  const t = tracker();
  await t.onTurnStart();
  await t.onBashStart("shell");
  fs.writeFileSync("created.txt", "shell content\n");
  await t.onBashEnd("shell");
  await t.finalizeTurn();
  const p = patches(t).find(p => p.file === "created.txt");
  assert.ok(p, "shell-created file must be discovered");
  assert.equal(p.newHash, SnapshotStore.hash(Buffer.from("shell content\n")));
  assert.equal(t.coverageLimited(), false);
});

test("entry budget stops further automatic scans without inventing deletions", async () => {
  fixture("large");
  manyFiles();
  fs.writeFileSync("target.txt", "old\n");
  const t = tracker();
  await t.onTurnStart();
  await t.onBashStart("first");
  limited(t);
  // Once coverage is limited, neither later shells nor finalize may restart a tree walk.
  t.walk = async () => assert.fail("tree scan resumed after entry budget exhaustion");
  await t.recordFileChange("target.txt", { fromToolEvent: true });
  fs.writeFileSync("target.txt", "new\n");
  await t.onBashEnd("first");
  await t.onBashStart("second");
  await t.onBashEnd("second");
  await t.finalizeTurn();
  exact(t, "target.txt", "old\n", "new\n");
  assert.ok(patches(t).every(p => p.status !== "deleted"), "partial inventories cannot prove deletion");
});

for (const phase of ["baseline", "shell-after", "finalize"]) {
  test(`${phase} has a deadline and stops scheduling after suspended directory I/O`, async () => {
    const cwd = fixture(`stall-${phase}`);
    fs.mkdirSync("later");
    fs.writeFileSync("later/sentinel.txt", "untouched");
    const t = tracker();
    await t.onTurnStart();
    if (phase !== "baseline") await t.onBashStart("shell");
    const original = fsp.opendir;
    let release;
    let drained;
    let entered = false;
    let subsequent = 0;
    const gate = new Promise(resolve => { release = resolve; });
    const settled = new Promise(resolve => { drained = resolve; });
    fsp.opendir = async (...args) => {
      if (path.resolve(String(args[0])) !== cwd) {
        subsequent++;
        return original(...args);
      }
      entered = true;
      await gate;
      const handle = await original(...args);
      return {
        async *[Symbol.asyncIterator]() {
          try { yield* handle; }
          finally { drained(); }
        },
      };
    };
    syncBuiltinESMExports();
    let operation;
    try {
      operation = phase === "baseline" ? t.onBashStart("shell")
        : phase === "shell-after" ? t.onBashEnd("shell") : t.finalizeTurn();
      await bounded(operation);
      assert.ok(entered, `${phase} must enter the real opendir suspension`);
      limited(t);
    } finally {
      release();
      try {
        await operation;
        if (entered) await bounded(settled);
        // Drain continuations after the suspended iterator closes before fixture cleanup.
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(subsequent, 0, "cancelled scan must not open subsequent directories");
      } finally {
        fsp.opendir = original;
        syncBuiltinESMExports();
      }
    }
  });
}

async function observeReads(cwd, run) {
  const original = fsp.open;
  const observation = { opened: [], bytes: 0, reads: 0 };
  fsp.open = async (...args) => {
    const handle = await original(...args);
    const file = path.resolve(String(args[0]));
    if (file.startsWith(cwd + path.sep)) {
      observation.opened.push(path.relative(cwd, file));
      const read = handle.read.bind(handle);
      handle.read = async (...readArgs) => {
        const result = await read(...readArgs);
        observation.bytes += result.bytesRead;
        observation.reads++;
        return result;
      };
    }
    return handle;
  };
  syncBuiltinESMExports();
  try { await run(observation); }
  finally { fsp.open = original; syncBuiltinESMExports(); }
}

function gitFixture(name) {
  const cwd = fixture(name);
  git("init", "-q");
  fs.writeFileSync("target.txt", "committed\n");
  commit();
  fs.writeFileSync("target.txt", "dirty\n");
  return cwd;
}

async function editKnown(t) {
  await t.recordFileChange("target.txt", { fromToolEvent: true });
  fs.writeFileSync("target.txt", "after\n");
  await t.onBashEnd("shell");
  await t.finalizeTurn();
  exact(t, "target.txt", "dirty\n", "after\n");
}

test("Git entry budget stops content reads and preserves explicit targets", async () => {
  const cwd = gitFixture("git-many");
  const count = manyFiles();
  const t = tracker();
  await observeReads(cwd, async observation => {
    await t.onTurnStart();
    await t.onBashStart("shell");
    limited(t);
    assert.ok(observation.opened.length < count, "entry-limited Git inventory must not open every file");
    await editKnown(t);
  });
});

test("small Git inventory excludes oversized content before open", async () => {
  const cwd = gitFixture("git-oversized");
  const fd = fs.openSync("huge.txt", "w");
  fs.ftruncateSync(fd, 128 * 1024 * 1024);
  fs.closeSync(fd);
  const t = tracker();
  await observeReads(cwd, async observation => {
    await t.onTurnStart();
    await t.onBashStart("shell");
    limited(t);
    assert.ok(!observation.opened.includes("huge.txt"), "oversized automatic content must be excluded before open");
    await editKnown(t);
    assert.ok(!observation.opened.includes("huge.txt"));
  });
});

test("Git total content budget bounds actual chunk bytes across small dirty files", async () => {
  const cwd = gitFixture("git-bytes");
  const size = Math.floor(SCAN_LIMITS.fileBytes * 0.75);
  const count = Math.ceil(SCAN_LIMITS.bytes / size) + 3;
  assert.ok(count < SCAN_LIMITS.entries, "fixture must isolate bytes from entry limit");
  for (let i = 0; i < count; i++) fs.writeFileSync(`dirty-${i}.txt`, Buffer.alloc(size, 97));
  const t = tracker();
  await observeReads(cwd, async observation => {
    await t.onTurnStart();
    await t.onBashStart("shell");
    limited(t);
    assert.ok(observation.reads > 0, "must exercise real FileHandle.read");
    assert.ok(observation.bytes >= SCAN_LIMITS.bytes, "must reach content budget rather than only a deadline");
    assert.ok(observation.bytes <= SCAN_LIMITS.bytes + SCAN_LIMITS.fileBytes,
      "one in-flight file is the maximum content-budget overshoot");
    assert.ok(observation.bytes < count * size, "must stop before reading the full dirty inventory");
    const bytes = observation.bytes;
    await editKnown(t);
    assert.ok(observation.bytes - bytes < 1024, "limited scans must read only the explicit small target");
  });
});

test("Git shell retains pre-dirty and clean old sides across stage and commit", async () => {
  fixture("git-small");
  git("init", "-q");
  fs.writeFileSync("dirty.txt", "committed\n");
  fs.writeFileSync("clean.txt", "clean old\n");
  commit();
  fs.writeFileSync("dirty.txt", "preexisting dirty\n");
  const t = tracker();
  await t.onTurnStart();
  await t.onBashStart("shell");
  fs.writeFileSync("dirty.txt", "dirty new\n");
  fs.writeFileSync("clean.txt", "clean new\n");
  commit();
  await t.onBashEnd("shell");
  await t.finalizeTurn();
  exact(t, "dirty.txt", "preexisting dirty\n", "dirty new\n");
  exact(t, "clean.txt", "clean old\n", "clean new\n");
});

function suspendedResult() {
  let release;
  let finish;
  const gate = new Promise(resolve => { release = resolve; });
  const settled = new Promise(resolve => { finish = resolve; });
  return { gate, release, finish, settled };
}

function publishedLimited(t, entries) {
  limited(t);
  assert.equal(t.turns().at(-1).coverageLimited, true);
  const restored = collectTurnDiffs(JSON.parse(JSON.stringify(entries)));
  assert.equal(restored.length, 1, "published turn must survive store validation");
  assert.equal(restored[0].coverageLimited, true, "restoration must retain incomplete coverage");
  assert.deepEqual(restored[0], t.turns().at(-1));
  return JSON.stringify({ turns: t.turns(), entries });
}

test("shell change registration shares one deadline and cannot publish late old sides", async () => {
  fixture("git-registration-stall");
  git("init", "-q");
  for (const file of ["a.txt", "b.txt", "c.txt"]) fs.writeFileSync(file, `old ${file}\n`);
  commit();
  const entries = [];
  const t = tracker(entries);
  await t.onTurnStart();
  await t.onBashStart("shell");
  for (const file of ["a.txt", "b.txt", "c.txt"]) fs.writeFileSync(file, `new ${file}\n`);
  const original = Git.showRevision;
  const suspended = suspendedResult();
  const calls = [];
  Git.showRevision = async (...args) => {
    calls.push(args[2]);
    try {
      const content = await original(...args);
      if (calls.length === 1) await suspended.gate;
      return content;
    } finally { suspended.finish(); }
  };
  let operation;
  let published;
  try {
    operation = t.onBashEnd("shell");
    await bounded(operation);
    assert.equal(calls.length, 1, "must reach the first real old-side lookup and stop before a second");
    limited(t);
    await bounded(t.finalizeTurn());
    published = publishedLimited(t, entries);
  } finally {
    suspended.release();
    try {
      await operation;
      if (calls.length) await bounded(suspended.settled);
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(calls.length, 1, "expired aggregate registration must not read another file");
      if (published) assert.equal(JSON.stringify({ turns: t.turns(), entries }), published,
        "late old-side completion must not mutate published turn state");
    } finally { Git.showRevision = original; }
  }
});

test("patch generation deadline preserves unavailable known files and immutable persisted output", async () => {
  fixture("snapshot-stall");
  const entries = [];
  const t = tracker(entries);
  for (const file of ["a.txt", "b.txt"]) fs.writeFileSync(file, `old ${file}\n`);
  await t.onTurnStart();
  for (const file of ["a.txt", "b.txt"]) {
    await t.recordFileChange(file, { fromToolEvent: true });
    fs.writeFileSync(file, `new ${file}\n`);
  }
  const original = t.snapshotStore.get.bind(t.snapshotStore);
  const suspended = suspendedResult();
  let calls = 0;
  t.snapshotStore.get = async (...args) => {
    calls++;
    try {
      const content = await original(...args);
      if (calls === 1) await suspended.gate;
      return content;
    } finally { suspended.finish(); }
  };
  let operation;
  let published;
  try {
    operation = t.finalizeTurn();
    await bounded(operation);
    assert.equal(calls, 1, "must enter the real old-snapshot suspension");
    published = publishedLimited(t, entries);
    assert.deepEqual(patches(t).map(p => p.file).sort(), ["a.txt", "b.txt"],
      "deadline must retain every known changed file");
    for (const patch of patches(t)) {
      assert.equal(patch.unavailable, true, "remaining known files must explicitly report unavailable content");
      assert.equal(patch.status, "modified", "unfinished patch generation cannot prove deletion or no change");
      assert.equal(patch.newHash, undefined, "unfinished patches cannot claim a complete new side");
    }
  } finally {
    suspended.release();
    try {
      await operation;
      if (calls) await bounded(suspended.settled);
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(calls, 1, "expired patch generation must not retrieve another snapshot");
      if (published) assert.equal(JSON.stringify({ turns: t.turns(), entries }), published,
        "late snapshot completion must not mutate persisted patches");
    } finally { t.snapshotStore.get = original; }
  }
});

test("expired finalize file read cannot limit the next turn or suppress its shell discovery", async () => {
  const cwd = fixture("finalize-epoch-stall");
  const target = path.join(cwd, "target.txt");
  fs.writeFileSync(target, "old\n");
  const entries = [];
  const t = tracker(entries);
  await t.onTurnStart();
  await t.recordFileChange("target.txt", { fromToolEvent: true });
  fs.writeFileSync(target, "new\n");
  const original = fsp.stat;
  const suspended = suspendedResult();
  let entered = false;
  fsp.stat = async (...args) => {
    // Metadata inventory uses bigint stats; suspend the actual content-read stat only.
    if (!entered && path.resolve(String(args[0])) === target && !args[1]?.bigint) {
      entered = true;
      try {
        await suspended.gate;
        return await original(...args);
      } finally { suspended.finish(); }
    }
    return original(...args);
  };
  syncBuiltinESMExports();
  let operation;
  try {
    operation = t.finalizeTurn();
    await bounded(operation);
    assert.ok(entered, "must suspend the registered target's finalize content-read stat");
    const published = publishedLimited(t, entries);
    await t.onTurnStart();
    assert.equal(t.coverageLimited(), false, "new turn must start with fresh coverage");
    suspended.release();
    await bounded(suspended.settled);
    // Let the expired read reject through readFileOrNull and the finalize callback catch.
    await new Promise(resolve => setImmediate(resolve));
    const limitedAfterOldCallback = t.coverageLimited();
    assert.equal(JSON.stringify({ turns: t.turns(), entries }), published,
      "expired old-turn I/O must not mutate published patches");
    await t.onBashStart("new-turn-shell");
    fs.writeFileSync("next.txt", "next turn shell\n");
    await t.onBashEnd("new-turn-shell");
    await t.finalizeTurn();
    assert.equal(limitedAfterOldCallback, false, "expired finalize catch must not poison next-turn coverage");
    assert.equal(t.coverageLimited(), false, "new-turn shell coverage must remain complete");
    assert.ok(!t.turns().at(-1).coverageLimited, "new persisted turn must not inherit the old coverage flag");
    const created = patches(t).find(patch => patch.file === "next.txt");
    assert.ok(created, "new turn must still discover shell-created files after old I/O completes");
    assert.equal(created.newHash, SnapshotStore.hash(Buffer.from("next turn shell\n")));
  } finally {
    suspended.release();
    try {
      await operation;
      if (entered) await bounded(suspended.settled);
      await new Promise(resolve => setImmediate(resolve));
    } finally {
      fsp.stat = original;
      syncBuiltinESMExports();
    }
  }
});

try {
  for (const { name, run } of tests) {
    try { await run(); console.log(`PASS ${name}`); }
    catch (error) { failures++; console.error(`FAIL ${name}\n${error.stack}`); }
  }
} finally {
  process.chdir(originalCwd);
  fs.rmSync(root, { recursive: true, force: true });
  for (const key of Object.keys(process.env)) delete process.env[key];
  Object.assign(process.env, originalEnv);
}
console.log(`${tests.length - failures}/${tests.length} passed`);
process.exitCode = failures ? 1 : 0;
