#!/usr/bin/env python3
"""Real Pi CLI/TUI smoke test for context and optional diff views.

Usage: python3 pi-context-tui.py <node> <pi-cli.js> <contextPackage> [diffPackage absoluteGit [JSON sourcePaths [JSON keybindings]]]
"""
import fcntl
import json
import os
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

NODE, CLI, CONTEXT_PACKAGE = sys.argv[1:4]
DIFF_PACKAGE = sys.argv[4] if len(sys.argv) > 4 else None
GIT = sys.argv[5] if len(sys.argv) > 5 else None
SOURCE_PATHS = json.loads(sys.argv[6]) if len(sys.argv) > 6 else []
assert isinstance(SOURCE_PATHS, list) and all(
    isinstance(source, str) and os.path.isabs(source) and "cron" not in source.lower()
    for source in SOURCE_PATHS
), "sourcePaths must be an array of absolute non-cron extension paths"
KEYBINDINGS = json.loads(sys.argv[7]) if len(sys.argv) > 7 else None
BTW_PACKAGE = next((source for source in SOURCE_PATHS if os.path.isfile(os.path.join(source, "btw-ui.ts"))), None)
GOAL_PACKAGE = next((source for source in SOURCE_PATHS if os.path.isfile(os.path.join(source, "extensions/goal-widget.ts"))), None)
HAS_SCROLL_VIEWS = bool(BTW_PACKAGE and GOAL_PACKAGE)
HAS_FOOTER = any("pi-powerline" in os.path.basename(source) for source in SOURCE_PATHS)
HAS_QUEUE = any(os.path.isfile(os.path.join(source, "queue-state.ts")) for source in SOURCE_PATHS)
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")

def plain(data: bytes) -> str:
    return ANSI.sub("", data.decode("utf-8", "replace"))

def main() -> int:
    with tempfile.TemporaryDirectory(prefix="envy-pi-context-tui-") as root:
        return run_test(root)

def run_test(root: str) -> int:
    home = os.path.join(root, "home")
    tmp = os.path.join(root, "tmp")
    agent = os.path.join(root, "agent")
    project = os.path.join(root, "project")
    for directory in (
        home, tmp, agent, project,
        os.path.join(home, ".config"), os.path.join(home, ".cache"),
        os.path.join(home, ".local/share"), os.path.join(home, ".local/state"),
    ):
        os.makedirs(directory, exist_ok=True)
    if KEYBINDINGS is not None:
        assert isinstance(KEYBINDINGS, dict), "keybindings must be a JSON object"
        with open(os.path.join(agent, "keybindings.json"), "w", encoding="utf-8") as handle:
            json.dump(KEYBINDINGS, handle)
    frame_log = os.path.join(root, "scroll-frames.jsonl")
    interaction_fixture = ""
    if HAS_SCROLL_VIEWS:
        # Import the deployed components, while keeping all content and state offline.
        interaction_fixture = r'''
const { showBtwOverlay } = await import(BTW_UI);
const { GoalWidgetComponent } = await import(GOAL_WIDGET);
const { syncTerminalInputPause } = await import(GOAL_INPUT);
const { createGoal } = await import(GOAL_RECORD);
let unsubscribe;
let activeWidget;
function transcript(tui) {
  const visit = node => {
    if (node?.primary === true && typeof node.scrollTop === "number") return node;
    for (const child of node?.children ?? []) { const found = visit(child); if (found) return found; }
  };
  const found = tui.getMountedRoots().map(visit).find(Boolean);
  if (!found) throw new Error("Scroll regression requires the real fullscreen transcript");
  return found;
}
function observe(component, kind, ctx, tui) {
  const render = component.render.bind(component);
  component.render = width => {
    const lines = render(width);
    const chat = transcript(tui);
    fs.appendFileSync(FRAME_LOG, JSON.stringify({kind, lines, scrollTop: chat.scrollTop,
      history: JSON.stringify(ctx.sessionManager.getBranch())}) + "\n");
    return lines;
  };
  return component;
}
pi.registerCommand("scroll-history", { handler: async () => {
  pi.sendMessage({customType: "scroll-history", content: Array.from({length: 90},
    (_, i) => "MAIN-HISTORY-" + String(i).padStart(3, "0")).join("\n"), display: true});
} });
pi.registerCommand("scroll-btw", { handler: async (_, ctx) => {
  const watched = {...ctx, ui: {...ctx.ui, custom: (factory, options) =>
    ctx.ui.custom((tui, theme, kb, done) => observe(factory(tui, theme, kb, done), "btw", ctx, tui), options)}};
  const shown = showBtwOverlay({ctx: watched, question: "Offline scrolling regression", history: [],
    controller: new AbortController(), onClearHistory() {}});
  (await shown.controllerReady).setAnswer(Array.from({length: 100},
    (_, i) => "BTW-LINE-" + String(i).padStart(3, "0")).join("\n"));
  await shown.overlayPromise;
} });
pi.registerCommand("scroll-goal", { handler: async (_, ctx) => {
  const goal = createGoal({objective: "Offline compact overflow regression", autoContinue: false});
  goal.taskList = {tasks: Array.from({length: 30}, (_, i) => ({id: "task-" + i,
    title: "GOAL-TASK-" + String(i).padStart(3, "0"), status: "pending"})), blockCompletion: false};
  const core = {goalModalDepth: 0, state: {goal}, auditProgress: null,
    isDashboardExpanded: () => false, goalWidgetComponentRef: {current: null}};
  ctx.ui.setWidget("scroll-regression", (tui, theme) => {
    activeWidget = observe(new GoalWidgetComponent({tui, theme, getGoal: () => goal}), "goal", ctx, tui);
    core.goalWidgetComponentRef.current = activeWidget;
    return activeWidget;
  });
  syncTerminalInputPause(core, ctx);
  unsubscribe = () => core.terminalInputUnsubscribe?.();
} });
pi.registerCommand("scroll-clear", { handler: async (_, ctx) => {
  unsubscribe?.(); activeWidget = undefined;
  ctx.ui.setWidget("scroll-regression", undefined);
  ctx.ui.notify("Scroll fixture closed", "info");
} });
'''
        for token, value in {
            "BTW_UI": os.path.join(BTW_PACKAGE, "btw-ui.ts"),
            "GOAL_WIDGET": os.path.join(GOAL_PACKAGE, "extensions/widgets/goal-widget.ts"),
            "GOAL_INPUT": os.path.join(GOAL_PACKAGE, "extensions/goal-widget.ts"),
            "GOAL_RECORD": os.path.join(GOAL_PACKAGE, "extensions/goal-record.ts"),
            "FRAME_LOG": frame_log,
        }.items():
            interaction_fixture = interaction_fixture.replace(token, json.dumps(value))
    request_log = os.path.join(root, "requests.log")
    fixture = os.path.join(root, "offline-fixture.ts")
    with open(fixture, "w", encoding="utf-8") as handle:
        handle.write(f'''
import fs from "node:fs";
import {{ createAssistantMessageEventStream }} from "@earendil-works/pi-ai";
export default async function(pi) {{
{interaction_fixture}
  pi.registerCommand("footer-theme", {{ handler: async (args, ctx) => {{
    ctx.ui.setTheme(args.trim() === "light" ? "light" : "dark");
    ctx.ui.notify("Footer theme switched: " + args.trim(), "info");
  }} }});
  pi.registerProvider("envy-offline", {{
    baseUrl: "http://invalid.test", apiKey: "offline-test-only", api: "envy-offline-api",
    streamSimple: (model) => {{
      fs.appendFileSync({request_log!r}, "request\\n");
      const stream = createAssistantMessageEventStream();
      const message = {{
        role: "assistant", api: model.api, provider: model.provider, model: model.id,
        timestamp: Date.now(), stopReason: "stop",
        usage: {{ input: 12, output: 3, cacheRead: 0, cacheWrite: 0, totalTokens: 15,
          cost: {{ input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0.125 }} }},
        content: [{{ type: "text", text: "Offline context reply" }}],
      }};
      stream.push({{ type: "start", partial: message }});
      stream.push({{ type: "text_start", contentIndex: 0, partial: message }});
      stream.push({{ type: "text_delta", contentIndex: 0, delta: "Offline context reply", partial: message }});
      stream.push({{ type: "text_end", contentIndex: 0, content: "Offline context reply", partial: message }});
      stream.push({{ type: "done", reason: "stop", message }});
      stream.end();
      return stream;
    }},
    models: [{{ id: "inherited", name: "inherited", reasoning: false, input: ["text"],
      contextWindow: 200000, maxTokens: 4096,
      cost: {{ input: 0, output: 0, cacheRead: 0, cacheWrite: 0 }} }}],
  }});
}}
''')
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": home, "TMPDIR": tmp,
        "XDG_CONFIG_HOME": os.path.join(home, ".config"),
        "XDG_CACHE_HOME": os.path.join(home, ".cache"),
        "XDG_DATA_HOME": os.path.join(home, ".local/share"),
        "XDG_STATE_HOME": os.path.join(home, ".local/state"),
        "PI_CODING_AGENT_DIR": agent,
        "TERM": "xterm-256color",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }
    if DIFF_PACKAGE:
        assert GIT and os.path.isabs(GIT), "diff requires an absolute git path"
        env["PATH"] = os.path.dirname(GIT) + ":" + env["PATH"]
        def git(*args):
            return subprocess.check_output([GIT, *args], cwd=project, env=env)
        git("init", "-q")
        sample = os.path.join(project, "sample.txt")
        with open(sample, "wb") as handle:
            handle.write(b"before-envy-diff\n")
        git("add", "sample.txt")
        with open(sample, "wb") as handle:
            handle.write(b"after-envy-diff\n")
        with open(os.path.join(project, ".git", "index"), "rb") as handle:
            index_before = handle.read()
    extension_paths = list(dict.fromkeys(
        os.path.realpath(source)
        for source in [CONTEXT_PACKAGE, fixture, *([DIFF_PACKAGE] if DIFF_PACKAGE else []), *SOURCE_PATHS]
    ))
    extension_args = [argument for source in extension_paths for argument in ("--extension", source)]
    if HAS_FOOTER:
        printed = subprocess.run(
            [NODE, CLI, "--offline", "--print", "--no-tools", "--no-session",
             "--provider", "envy-offline", "--model", "inherited", *extension_args, "plain footer check"],
            cwd=project, env=env, capture_output=True, timeout=30, check=True,
        )
        assert b"Offline context reply" in printed.stdout
        assert b"\x1b[" not in printed.stdout and b"\x1b]" not in printed.stdout, "footer polluted print output"
        os.unlink(request_log)
    session_args = ["--session", os.path.join(root, "footer-session.jsonl")] if HAS_FOOTER else ["--no-session"]
    master, slave = pty.openpty()
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 120, 0, 0))
        process = subprocess.Popen(
            [NODE, CLI, "--offline", "--no-tools", *session_args,
             "--provider", "envy-offline", "--model", "inherited",
             *extension_args],
            cwd=project, env=env, stdin=slave, stdout=slave, stderr=slave,
            close_fds=True, start_new_session=True,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    output = bytearray()

    def read_until(pattern: str, timeout: float = 30.0, since: int = 0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            text = plain(bytes(output[since:]))
            if re.search(pattern, text):
                return text
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                output.extend(chunk)
            if process.poll() is not None:
                break
        raise AssertionError(f"timed out waiting for {pattern!r}; tail:\n{plain(bytes(output)[since:])[-4000:]}")

    def send(text: str) -> None:
        os.write(master, text.encode())

    def request_count() -> int:
        if not os.path.exists(request_log):
            return 0
        with open(request_log, encoding="utf-8") as handle:
            return sum(1 for line in handle if line.strip())

    def close_view(key: str, marker: str, expected_requests: int) -> None:
        mark = len(output)
        send(key)
        read_until(r"inherited", since=mark)
        mark = len(output)
        send(marker)
        read_until(re.escape(marker), since=mark)
        if request_count() != expected_requests:
            raise AssertionError("closing the context view made a model request")
        send("\x15")

    def scroll_frames(kind: str) -> list[dict]:
        if not os.path.exists(frame_log):
            return []
        with open(frame_log, encoding="utf-8") as handle:
            return [frame for line in handle if line.endswith("\n")
                    and (frame := json.loads(line))["kind"] == kind]

    def wait_frame(kind: str, predicate, after: int = 0) -> dict:
        deadline = time.monotonic() + 10
        last_count, changed_at = -1, time.monotonic()
        while time.monotonic() < deadline:
            frames = scroll_frames(kind)
            if len(frames) != last_count:
                last_count, changed_at = len(frames), time.monotonic()
            if len(frames) > after and time.monotonic() - changed_at >= 0.2 and predicate(frames[-1]):
                return frames[-1]
            ready, _, _ = select.select([master], [], [], 0.1)
            if ready:
                try:
                    output.extend(os.read(master, 65536))
                except OSError:
                    break
            if process.poll() is not None:
                break
        recent = [{**frame, "history": "<omitted>"} for frame in scroll_frames(kind)[-2:]]
        raise AssertionError(f"missing {kind} scroll frame; frames={recent}; "
                             f"tail={plain(bytes(output))[-3000:]}")

    def visible_ids(frame: dict, prefix: str) -> list[int]:
        # The compact dashboard repeats the current task outside its viewport.
        lines = [line for line in frame["lines"] if "Current" not in plain(line.encode())]
        return [int(value) for value in re.findall(prefix + r"(\d{3})", "\n".join(lines))]

    def scroll_key(kind: str, key: str, baseline: dict, expected) -> dict:
        before = len(scroll_frames(kind))
        send(key)
        frame = wait_frame(kind, expected, before)
        assert frame["scrollTop"] == baseline["scrollTop"], f"{kind} key moved main history"
        assert frame["history"] == baseline["history"], f"{kind} key changed session history"
        assert request_count() == 1, f"{kind} scrolling made a model request"
        return frame

    def wait_for_exit(timeout: float = 15.0) -> None:
        deadline = time.monotonic() + timeout
        while process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                output.extend(chunk)
        if process.poll() is None:
            raise AssertionError(f"Pi did not exit; tail:\n{plain(bytes(output))[-4000:]}")

    try:
        read_until(r"\[Extensions\]", 45)
        mark = len(output)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 51, 120, 0, 0))
        process.send_signal(signal.SIGWINCH)
        initial_footer = r"inherited.*0\.0%/200k \(auto\)" if HAS_FOOTER else r"0\.0%/200k \(auto\).*inherited"
        read_until(initial_footer, 45, mark)
        # No request yet: opening Usage must show the explicit degraded estimate.
        mark = len(output)
        send("/context usage\r")
        read_until(r"Context Usage", since=mark)
        read_until(r"Automatic model probing is disabled by envY", since=mark)
        if request_count() != 0:
            raise AssertionError("opening /context usage before a turn made a model request")
        close_view("q", "envy-editor-before-turn", 0)
        # A natural fake-provider turn publishes a real request snapshot.
        mark = len(output)
        send("hello offline\r")
        read_until(r"Offline context reply", 45, mark)
        if request_count() != 1:
            raise AssertionError(f"expected one natural model request, got {request_count()}")
        mark = len(output)
        send("/context usage\r")
        read_until(r"Context Usage", since=mark)
        read_until(r"System Prompt", since=mark)
        if request_count() != 1:
            raise AssertionError("opening /context usage after a turn made a model request")
        close_view("q", "envy-editor-after-usage", 1)
        mark = len(output)
        send("/context injections\r")
        read_until(r"Context Injections", since=mark)
        read_until(r"Latest Request", since=mark)
        read_until(r"System Prompt", since=mark)
        read_until(r"TOTAL \(Prompt, Tools, Injections\)", since=mark)
        if request_count() != 1:
            raise AssertionError("opening /context injections made a model request")
        close_view("\x1b", "envy-editor-after-injections", 1)
        if DIFF_PACKAGE:
            mark = len(output)
            send("/diff\r")
            read_until(r"sample\.txt", since=mark)
            read_until(r"before-envy-diff", since=mark)
            read_until(r"after-envy-diff", since=mark)
            mark = len(output)
            send("v")
            read_until(r"Unified view|Split view", since=mark)
            mark = len(output)
            send("\t")
            read_until(r"Uncommitted", since=mark)
            close_view("\x1b", "envy-editor-after-diff", 1)
            mark = len(output)
            send("/diff\r")
            read_until(r"sample\.txt", since=mark)
            close_view("q", "envy-editor-after-diff-q", 1)
            with open(sample, "rb") as handle:
                assert handle.read() == b"after-envy-diff\n"
            with open(os.path.join(project, ".git", "index"), "rb") as handle:
                assert handle.read() == index_before, "diff changed the Git index"
        if HAS_FOOTER:
            read_until(r"\$0\.125")
            for columns in (24, 40, 120):
                mark = len(output)
                fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 51, columns, 0, 0))
                process.send_signal(signal.SIGWINCH)
                read_until(r"inherited", since=mark)
                if process.poll() is not None:
                    raise AssertionError("footer crashed during terminal resize")
            mark = len(output)
            send("中文编辑器检查")
            read_until("中文编辑器检查", since=mark)
            send("\x15")
            for theme in ("light", "dark"):
                mark = len(output)
                send("/footer-theme " + theme + "\r")
                read_until("Footer theme switched: " + theme, since=mark)
            if request_count() != 1:
                raise AssertionError("footer interactions made a model request")
        if HAS_SCROLL_VIEWS:
            # A tall real transcript makes accidental host scrolling observable.
            mark = len(output)
            send("/scroll-history\r")
            read_until("MAIN-HISTORY-089", since=mark)
            send("/scroll-btw\r")
            bottom = wait_frame("btw", lambda frame: 99 in visible_ids(frame, "BTW-LINE-"))
            assert bottom["scrollTop"] > 0, "main history must overflow for scroll isolation"
            oldest = min(visible_ids(bottom, "BTW-LINE-"))
            scroll_key("btw", "\x1b[A", bottom,
                            lambda frame: min(visible_ids(frame, "BTW-LINE-")) == oldest - 1)
            scroll_key("btw", "\x1b[B", bottom, lambda frame: frame["lines"] == bottom["lines"])
            scroll_key("btw", "\x1b[5~", bottom,
                              lambda frame: min(visible_ids(frame, "BTW-LINE-")) < oldest - 1)
            scroll_key("btw", "\x1b[6~", bottom, lambda frame: frame["lines"] == bottom["lines"])
            scroll_key("btw", "\x1b[H", bottom, lambda frame: 0 in visible_ids(frame, "BTW-LINE-"))
            scroll_key("btw", "\x1b[F", bottom, lambda frame: frame["lines"] == bottom["lines"])
            # SGR wheel events hit the real overlay mouse dispatcher.
            scroll_key("btw", "\x1b[<64;20;35M", bottom,
                       lambda frame: min(visible_ids(frame, "BTW-LINE-")) < oldest)
            scroll_key("btw", "\x1b[<65;20;35M", bottom, lambda frame: frame["lines"] == bottom["lines"])
            close_view("\x1b", "envy-editor-after-btw-scroll", 1)
            send("/scroll-goal\r")
            initial = wait_frame("goal", lambda frame: bool(visible_ids(frame, "GOAL-TASK-")))
            first = min(visible_ids(initial, "GOAL-TASK-"))
            scroll_key("goal", "\x1b[1;6B", initial,
                       lambda frame: min(visible_ids(frame, "GOAL-TASK-")) == first + 1)
            scroll_key("goal", "\x1b[1;6A", initial, lambda frame: frame["lines"] == initial["lines"])
            # Main-chat Ctrl+Up still navigates prompts without moving the goal list.
            before = len(scroll_frames("goal"))
            send("\x1b[1;5A")
            moved = wait_frame("goal", lambda frame: frame["scrollTop"] < initial["scrollTop"], before)
            assert moved["lines"] == initial["lines"], "main-chat scroll changed compact goal"
            assert moved["history"] == initial["history"], "main-chat scroll changed session history"
            before = len(scroll_frames("goal"))
            send("\x1b[1;5F")
            wait_frame("goal", lambda frame: frame["scrollTop"] == initial["scrollTop"], before)
            mark = len(output)
            send("/scroll-clear\r")
            read_until("Scroll fixture closed", since=mark)
            mark = len(output)
            send("envy-editor-after-goal-scroll")
            read_until("envy-editor-after-goal-scroll", since=mark)
            send("\x15")
            assert request_count() == 1, "scroll fixture or editor recovery made a model request"
        if HAS_QUEUE:
            # Exercise argument completion and command dispatch through the real editor.
            mark = len(output)
            send("/queue ad")
            read_until(r"add <text>", since=mark)
            send("\t")
            send("TUI-queued-中文\r")
            read_until(r"Queued follow-up-1", since=mark)
            assert request_count() == 1, "idle command enqueue made a model request"
            mark = len(output)
            send("/queue edit follow")
            read_until(r"follow-up-1", since=mark)
            send("\t\r")
            read_until(r"Editing queue row", since=mark)
            mark = len(output)
            send("\x15TUI-edited-中文\r")
            read_until(r"TUI-edited-中文", since=mark)
            mark = len(output)
            send("/queue remove follow-up-1\r")
            read_until(r"Removed follow-up-1", since=mark)
            mark = len(output)
            send("/queue add Clear-cancel-row\r")
            read_until(r"Queued follow-up-2", since=mark)
            mark = len(output)
            send("/queue clear\r")
            read_until(r"Clear queue\?", since=mark)
            mark = len(output)
            send("\x1b")
            read_until(r"Queued follow-up-2", since=mark)
            mark = len(output)
            send("/queue list\r")
            read_until(r"Queue: 1 pending; paused", since=mark)
            mark = len(output)
            send("/queue remove follow-up-2\r")
            read_until(r"Removed follow-up-2", since=mark)
            mark = len(output)
            send("/queue\r")
            read_until(r"Queue: 0 pending", since=mark)
            read_until(r"Tab completes subcommands and row IDs", since=mark)
            assert request_count() == 1, "queue controls or completion made a model request"
        send("/quit\r")
        wait_for_exit()
        if process.returncode != 0:
            raise AssertionError(f"Pi exited with {process.returncode}; tail:\n{plain(bytes(output))[-4000:]}")
        if HAS_FOOTER:
            if b"\x1b[?25h" not in output:
                raise AssertionError("terminal cursor was not restored on exit")
            output.clear()
            master2, slave2 = pty.openpty()
            os.close(master)
            master = master2
            fcntl.ioctl(slave2, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 120, 0, 0))
            try:
                process = subprocess.Popen(
                    [NODE, CLI, "--offline", "--no-tools", *session_args,
                     "--provider", "envy-offline", "--model", "inherited", *extension_args],
                    cwd=project, env=env, stdin=slave2, stdout=slave2, stderr=slave2,
                    close_fds=True, start_new_session=True,
                )
            finally:
                os.close(slave2)
            if not HAS_SCROLL_VIEWS:
                read_until(r"\[Extensions\]", 45)
            read_until(r"master inherited.*\$0\.125", 45)
            if request_count() != 1:
                raise AssertionError("restoring footer state made a model request")
            send("/quit\r")
            wait_for_exit()
            assert process.returncode == 0, "restored Pi did not exit cleanly"
        print("PTY context" + (" + diff" if DIFF_PACKAGE else "") + (" + btw/goal scrolling" if HAS_SCROLL_VIEWS else "") + (" + queue commands" if HAS_QUEUE else "") + " TUI smoke passed; offline requests:", request_count())
        return 0
    finally:
        try:
            # Kill the isolated process group so failed checks leave no descendants.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                process.kill()
            process.wait()
        finally:
            os.close(master)

if __name__ == "__main__":
    raise SystemExit(main())
