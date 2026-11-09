#!/usr/bin/env python3
"""Offline Pi 1.1 PTY regression: node pi-cli.js diffPackage absoluteGit."""
import fcntl
import json
import os
from pathlib import Path
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time

ANSI = re.compile(rb"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
FIXTURE = r'''
import fs from "node:fs";
import path from "node:path";
import net from "node:net";
import tls from "node:tls";
import { syncBuiltinESMExports } from "node:module";
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";
const stamp = (stage, extra = {}) => fs.appendFileSync(LOG, JSON.stringify({stage, time: Date.now(), ...extra}) + "\n");
const deny = () => { stamp("network"); throw new Error("Network forbidden in offline regression"); };
globalThis.fetch = deny;
net.Socket.prototype.connect = deny;
tls.connect = deny;
let armed = false, submitted = false, release;
const gate = new Promise(resolve => { release = resolve; });
for (const name of ["readdir", "opendir"]) {
  const original = fs.promises[name];
  fs.promises[name] = async function(directory, options) {
    if (armed && path.resolve(String(directory)) === process.cwd() &&
        (name === "opendir" || options?.withFileTypes === true)) {
      stamp("scan", {name, beforeUser: !submitted});
      await gate;
    }
    return original.call(this, directory, options);
  };
}
syncBuiltinESMExports();
export default function(pi) {
  pi.on("input", () => { armed = true; stamp("input"); return {action:"continue"}; });
  pi.on("message_start", event => {
    if (event.message.role === "user") { submitted = true; stamp("user"); release(); }
  });
  pi.on("agent_settled", () => stamp("settled"));
  pi.registerProvider("latency-offline", {
    baseUrl:"http://invalid.test", apiKey:"offline-test-only", api:"latency-offline-api",
    streamSimple: model => {
      stamp("provider");
      const stream = createAssistantMessageEventStream();
      setTimeout(() => {
        stamp("reply");
        const message = {role:"assistant", api:model.api, provider:model.provider, model:model.id,
          timestamp:Date.now(), stopReason:"stop", content:[{type:"text", text:"LATENCY_OFFLINE_REPLY"}],
          usage:{input:12, output:3, cacheRead:0, cacheWrite:0, totalTokens:15,
            cost:{input:0, output:0, cacheRead:0, cacheWrite:0, total:0}}};
        stream.push({type:"start", partial:message});
        stream.push({type:"text_start", contentIndex:0, partial:message});
        stream.push({type:"text_delta", contentIndex:0, delta:"LATENCY_OFFLINE_REPLY", partial:message});
        stream.push({type:"text_end", contentIndex:0, content:"LATENCY_OFFLINE_REPLY", partial:message});
        stream.push({type:"done", reason:"stop", message}); stream.end();
      }, 1000);
      return stream;
    },
    models:[{id:"inherited", name:"inherited", reasoning:false, input:["text"], contextWindow:200000,
      maxTokens:4096, cost:{input:0, output:0, cacheRead:0, cacheWrite:0}}]
  });
}
'''


def run_case(root, name, node, cli, package, git):
    root.mkdir()
    for folder in ("home", "agent", "project", "tmp", "config", "cache", "data", "state"):
        (root / folder).mkdir()
    cwd = root / ("home" if name == "home" else "project")
    env = {"HOME":str(root / "home"), "TMPDIR":str(root / "tmp"),
           "PI_CODING_AGENT_DIR":str(root / "agent"), "PI_OFFLINE":"1", "TERM":"xterm-256color",
           "PATH":str(Path(git).parent) + ":/usr/bin:/bin:/usr/sbin:/sbin",
           "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":"/dev/null"}
    for key, folder in (("CONFIG", "config"), ("CACHE", "cache"), ("DATA", "data"), ("STATE", "state")):
        env[f"XDG_{key}_HOME"] = str(root / folder)
    if name == "git":
        subprocess.run([git, "init", "-q"], cwd=cwd, env=env, check=True)
    (cwd / "sample.txt").write_text("small fixture\n")
    log = root / "events.jsonl"
    log.write_text("")
    fixture = root / "fixture.ts"
    fixture.write_text("const LOG = " + json.dumps(str(log)) + ";\n" + FIXTURE)
    master, slave = pty.openpty()
    process = None
    output = bytearray()

    def pump(timeout=0.02):
        if select.select([master], [], [], timeout)[0]:
            try:
                output.extend(os.read(master, 65536))
            except OSError:
                pass

    def events():
        return [json.loads(line) for line in log.read_text().splitlines()]

    def until(predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            pump()
        raise AssertionError(f"{name}: timeout; events={events()}; terminal={ANSI.sub(b'', bytes(output))[-1500:]!r}")

    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 120, 0, 0))
        process = subprocess.Popen([node, cli, "--offline", "--no-tools", "--no-session",
            "--provider", "latency-offline", "--model", "inherited",
            "--extension", str(fixture), "--extension", package], cwd=cwd, env=env,
            stdin=slave, stdout=slave, stderr=slave, close_fds=True, start_new_session=True)
        os.close(slave)
        slave = None
        until(lambda: b"[Extensions]" in output and b"inherited" in output, 30)
        marker = ("SUBMITTED_LATENCY_" + name.upper()).encode()
        os.write(master, marker)
        until(lambda: marker in ANSI.sub(b"", bytes(output)))
        # Observe editor echo first, then discard all pending PTY bytes before Enter.
        quiet_until = time.monotonic() + 0.3
        while time.monotonic() < quiet_until:
            pump()
        mark = len(output)
        started = time.time() * 1000
        os.write(master, b"\r")
        visible = None
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            pump()
            if visible is None and marker in ANSI.sub(b"", bytes(output[mark:])):
                visible = time.time() * 1000
            if any(e["stage"] == "settled" for e in events()):
                break
        stages = events()
        scans = [e for e in stages if e["stage"] == "scan" and e["beforeUser"]]
        assert not scans, f"{name}: scan blocked submission before user message_start: {scans}"
        by_stage = {e["stage"]:e["time"] for e in stages}
        assert "user" in by_stage, f"{name}: missing actual user message_start: {stages}"
        latency = by_stage["user"] - started
        # Node Date.now() rounds down while the Python clock retains submilliseconds.
        assert -1 <= latency < 1000, (name, latency)
        assert visible is not None and started <= visible < by_stage["reply"], (name, visible, stages)
        assert by_stage["user"] <= by_stage["provider"] < by_stage["reply"]
        assert by_stage["reply"] - by_stage["provider"] >= 950, stages
        assert sum(e["stage"] == "provider" for e in stages) == 1, stages
        assert not any(e["stage"] == "network" for e in stages), stages
        assert "settled" in by_stage, stages
        until(lambda: b"LATENCY_OFFLINE_REPLY" in ANSI.sub(b"", bytes(output[mark:])))
        os.write(master, b"/quit\r")
        until(lambda: process.poll() is not None)
        assert process.returncode == 0, process.returncode
        print(json.dumps({"cwd":name, "userMessageMs":round(latency, 1),
            "freshSubmittedTextMs":round(visible-started, 1), "replyMs":round(by_stage["reply"]-started, 1),
            "scansBeforeUser":len(scans), "exit":process.returncode}), flush=True)
    finally:
        if slave is not None:
            os.close(slave)
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        os.close(master)


def main():
    if len(sys.argv) != 5:
        raise SystemExit("usage: pi-diff-latency.py <node> <pi-cli.js> <diffPackage> <absoluteGit>")
    node, cli, package, git = map(os.path.abspath, sys.argv[1:])
    assert os.path.isabs(sys.argv[4]), "Git path must be absolute"
    with tempfile.TemporaryDirectory(prefix="envy-pi-diff-latency-") as temporary:
        for name in ("home", "non-git", "git"):
            run_case(Path(temporary) / name, name, node, cli, package, git)


if __name__ == "__main__":
    main()
