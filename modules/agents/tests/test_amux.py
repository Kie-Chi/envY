"""Unit checks for the amux host client: input filtering, mounts, and commands."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

spec = importlib.util.spec_from_file_location(
    "amux", Path(os.environ.get("AMUX_PATH", str(Path(__file__).parents[1] / "amux/amux.py")))
)
amux = importlib.util.module_from_spec(spec)
sys.modules["amux"] = amux
spec.loader.exec_module(amux)


class InputFilterTests(unittest.TestCase):
    def filter(self, image=None):
        return amux.InputFilter(lambda: image)

    def test_tokens_keep_incomplete_escape(self):
        toks, rest = amux.tokens(b"ab\x1b[1;5")
        self.assertEqual(toks, [b"a", b"b"])
        self.assertEqual(rest, b"\x1b[1;5")
        toks, rest = amux.tokens(rest + b"A\x1bOP\x1b")
        self.assertEqual(toks, [b"\x1b[1;5A", b"\x1bOP"])
        self.assertEqual(rest, b"\x1b")

    def test_mouse_and_keys_pass_through(self):
        data = b"\x02d\x1b[<0;10;5M\x1b[98;5u\x1b"
        f = self.filter()
        self.assertEqual(f.feed(data) + f.flush(), data)

    def test_bracketed_paste_newlines_become_cr(self):
        f = self.filter()
        out = f.feed(b"\x1b[200~a\nb\r\nc\n\nd\x1b[201~\n")
        # CRLF stays one line break; blank lines survive.
        self.assertEqual(out, b"\x1b[200~a\rb\rc\r\rd\x1b[201~\n")

    def test_paste_split_across_reads(self):
        f = self.filter()
        out = f.feed(b"\x1b[20") + f.feed(b"0~x\n") + f.feed(b"y\x1b[201~")
        self.assertEqual(out, b"\x1b[200~x\ry\x1b[201~")

    def test_ctrl_v_injects_image_path(self):
        for key in (b"\x16", b"\x1b[118;5u", b"\x1b[27;5;118~"):
            out = self.filter("/amux/paste/clip.png").feed(key)
            self.assertEqual(out, b"\x1b[200~/amux/paste/clip.png\x1b[201~")

    def test_ctrl_v_without_image_is_forwarded(self):
        self.assertEqual(self.filter(None).feed(b"\x16"), b"\x16")


class MountTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    def plan(self, root, layout=None, add_dirs=(), skills=None, ssh=None, history=()):
        return amux.plan_mounts(
            root=root, home=self.home, layout=layout, add_dirs=list(add_dirs), skills=skills or {},
            etc="/nix/store/e-amux-etc", paste_dir=os.path.join(self.home, ".cache/amux/paste"), ssh_agent=ssh,
            history=history,
        )

    def test_refuses_home_and_ancestors(self):
        for path in (self.home, self.root, "/"):
            with self.assertRaises(amux.AmuxError):
                self.plan(path)
        with self.assertRaises(amux.AmuxError):
            self.plan(os.path.join(self.home, "p"), add_dirs=[(self.home, False)])

    def test_plain_directory(self):
        work = os.path.join(self.home, "work")
        os.makedirs(work)
        mounts = self.plan(work, add_dirs=[("/opt/data", False), (work, True)])
        self.assertIn(amux.Mount(work, work), mounts)
        self.assertIn(amux.Mount("/opt/data", "/opt/data", readonly=True), mounts)
        self.assertEqual([m.target for m in mounts].count(work), 1)
        self.assertNotIn("/nix/store", [m.target for m in mounts])
        self.assertIn(amux.Mount(amux.NIX_VOLUME, "/nix", volume=True), mounts)
        self.assertIn(amux.Mount("/nix/store/e-amux-etc", amux.ETC_MOUNT, readonly=True), mounts)
        self.assertIn(amux.Mount(amux.HOME_VOLUME, self.home, volume=True), mounts)
        self.assertIn(amux.PASTE_MOUNT, [m.target for m in mounts])

    def test_repository_protects_config_and_hooks(self):
        repo = os.path.join(self.home, "repo")
        os.makedirs(os.path.join(repo, "sub"))
        self.git("init", "-q", cwd=repo)
        layout = amux.git_layout(os.path.join(repo, "sub"), "git")
        self.assertEqual(layout.top, repo)
        mounts = self.plan(layout.top, layout)
        gitdir = os.path.join(repo, ".git")
        self.assertIn(amux.Mount(os.path.join(gitdir, "config"), os.path.join(gitdir, "config"), readonly=True), mounts)
        self.assertIn(amux.Mount(os.path.join(gitdir, "hooks"), os.path.join(gitdir, "hooks"), readonly=True), mounts)
        self.assertNotIn(gitdir, [m.target for m in mounts if not m.readonly])

    def test_worktree_mounts_common_dir(self):
        repo = os.path.join(self.home, "repo")
        os.makedirs(repo)
        self.git("init", "-q", cwd=repo)
        self.git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
        tree = os.path.join(self.root, "tree")
        self.git("worktree", "add", "-q", tree, cwd=repo)
        layout = amux.git_layout(tree, "git")
        mounts = self.plan(layout.top, layout)
        common = os.path.join(repo, ".git")
        self.assertIn(amux.Mount(tree, tree), mounts)
        self.assertIn(amux.Mount(common, common), mounts)
        self.assertNotIn(amux.Mount(layout.gitdir, layout.gitdir), mounts)
        self.assertIn(amux.Mount(os.path.join(common, "config"), os.path.join(common, "config"), readonly=True), mounts)

    def test_skills_mount_sources_and_ssh_agent(self):
        work = os.path.join(self.home, "work")
        source = os.path.join(self.root, "store-skill")
        target = os.path.join(self.home, ".claude/skills/read")
        os.makedirs(work)
        skills = {target: source}
        self.assertNotIn(target, [m.target for m in self.plan(work, skills=skills)])
        os.makedirs(source)
        mounts = self.plan(work, skills=skills, ssh="/run/host-services/ssh-auth.sock")
        self.assertIn(amux.Mount(source, target, readonly=True), mounts)
        targets = [m.target for m in mounts]
        self.assertLess(targets.index(self.home), targets.index(target))
        self.assertIn(amux.Mount("/run/host-services/ssh-auth.sock", amux.SSH_AGENT_MOUNT), mounts)

    def test_history_shares_host_directories_over_the_volume(self):
        work = os.path.join(self.home, "work")
        os.makedirs(work)
        history = amux.history_dirs("codex", self.home)
        self.assertEqual(history, [os.path.join(self.home, ".codex/sessions"),
                                   os.path.join(self.home, ".codex/archived_sessions")])
        self.assertEqual(os.stat(history[0]).st_mode & 0o777, 0o700)
        mounts = self.plan(work, history=history)
        targets = [m.target for m in mounts]
        for path in history:
            self.assertIn(amux.Mount(path, path), mounts)
            self.assertLess(targets.index(self.home), targets.index(path))
        self.assertEqual(amux.history_dirs("claude", self.home), [os.path.join(self.home, ".claude/projects")])

    def test_mount_argument_quotes_commas(self):
        self.assertEqual(amux.Mount("/a,b", "/a,b", readonly=True).args(),
                         ["--mount", 'type=bind,"source=/a,b","target=/a,b",readonly'])


class CommandTests(unittest.TestCase):
    CLAUDE = {"command": "claude", "skipArgs": ["--dangerously-skip-permissions"], "args": ["--system-prompt", "p"],
              "addDirFlag": "--add-dir"}
    CODEX = {"command": "codex", "skipArgs": ["--dangerously-bypass-approvals-and-sandbox"],
             "promptArgs": ["--ask-for-approval", "untrusted"], "args": []}

    def test_claude_argv(self):
        argv = amux.agent_argv(self.CLAUDE, "claude", {}, True, [("/data", False)], ["--resume"])
        self.assertEqual(argv, ["claude", "--dangerously-skip-permissions", "--system-prompt", "p",
                                "--add-dir", "/data", "--resume"])
        self.assertNotIn("--dangerously-skip-permissions", amux.agent_argv(self.CLAUDE, "claude", {}, False, [], []))

    def test_codex_provider_uses_env_key(self):
        env = {"OPENAI_BASE_URL": "https://example.test/v1", "OPENAI_MODEL": "m", "OPENAI_API_KEY": "secret"}
        argv = amux.agent_argv(self.CODEX, "codex", env, True, [], [])
        self.assertIn('model_providers.envy.base_url="https://example.test/v1"', argv)
        self.assertIn('model_providers.envy.env_key="OPENAI_API_KEY"', argv)
        self.assertEqual(argv[-2:], ["-m", "m"])
        self.assertFalse(any("secret" in arg for arg in argv))
        argv = amux.agent_argv(self.CODEX, "codex", {}, False, [], [])
        self.assertEqual(argv, ["codex", "--ask-for-approval", "untrusted"])
        argv = amux.agent_argv(self.CODEX, "codex", env, True, [], ["resume", "--last"], "custom")
        self.assertIn('model_provider="custom"', argv)
        self.assertIn('model_providers.custom.base_url="https://example.test/v1"', argv)
        self.assertEqual(argv[-4:], ["-m", "m", "resume", "--last"])

    def test_codex_provider_id_follows_host(self):
        with tempfile.TemporaryDirectory() as home:
            config = Path(home, ".codex/config.toml")
            self.assertEqual(amux.codex_provider_id(home), "envy")
            config.parent.mkdir()
            for text, expected in (('model_provider = "custom"\n', "custom"), ('model_provider = "openai"\n', "envy"),
                                   ('model_provider = "a.b"\n', "envy"), ("model_provider = [\n", "envy"),
                                   ('model = "m"\n', "envy")):
                config.write_text(text)
                self.assertEqual(amux.codex_provider_id(home), expected, text)

    def test_resumed_session(self):
        sid = "01a13348-d933-7910-8841-02f50eed042d"
        self.assertEqual(amux.resumed_session("codex", ["resume", sid]), sid)
        self.assertEqual(amux.resumed_session("codex", ["-m", "x", "fork", sid, "go on"]), sid)
        self.assertIsNone(amux.resumed_session("codex", ["resume", "--last"]))
        self.assertIsNone(amux.resumed_session("codex", [sid]))
        self.assertEqual(amux.resumed_session("claude", ["--fork-session", "-r", sid]), sid)
        self.assertEqual(amux.resumed_session("claude", [f"--resume={sid}"]), sid)
        self.assertIsNone(amux.resumed_session("claude", ["--resume", "title words"]))
        self.assertIsNone(amux.resumed_session("claude", ["--resume"]))

    def test_session_workdir_reads_host_history(self):
        sid = "01a13348-d933-7910-8841-02f50eed042d"
        with tempfile.TemporaryDirectory() as home:
            day = Path(home, ".codex/sessions/2026/10/13")
            day.mkdir(parents=True)
            meta = {"type": "session_meta", "payload": {"id": sid, "cwd": "/w/codex"}}
            Path(day, f"rollout-2026-10-13T11-22-54-{sid}.jsonl").write_text(json.dumps(meta) + "\n")
            Path(day, f"rollout-2026-10-13T11-30-00-x_{sid[:-1]}0.jsonl").write_text(json.dumps(meta) + "\n")
            self.assertEqual(amux.session_workdir("codex", home, sid.upper()), "/w/codex")
            self.assertIsNone(amux.session_workdir("codex", home, sid[:-1] + "1"))
            project = Path(home, ".claude/projects/-w-claude")
            project.mkdir(parents=True)
            lines = ["not json", json.dumps({"type": "summary"}), json.dumps({"type": "user", "cwd": "/w/claude"})]
            Path(project, f"{sid}.jsonl").write_text("\n".join(lines) + "\n")
            self.assertEqual(amux.session_workdir("claude", home, sid), "/w/claude")
            self.assertIsNone(amux.session_workdir("claude", home, sid[:-1] + "1"))

    def test_host_timezone(self):
        with mock.patch.dict(os.environ, {"TZ": ":Asia/Shanghai"}):
            self.assertEqual(amux.host_timezone(), "Asia/Shanghai")
        with mock.patch.dict(os.environ, {"TZ": "../etc/passwd"}):
            self.assertIsNone(amux.host_timezone())
        with mock.patch.dict(os.environ, {"TZ": ""}), \
                mock.patch.object(amux.os.path, "realpath", return_value="/var/db/timezone/zoneinfo/Europe/Paris"):
            self.assertEqual(amux.host_timezone(), "Europe/Paris")

    def test_load_env_keeps_selected_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp, "a.env"), Path(tmp, "b.env")
            first.write_text("export A=1\nexport B='two words'\nexport C=3\n")
            second.write_text("unset C\nexport A=override\n")
            env = amux.load_env([str(first), str(second), str(Path(tmp, "missing"))], ["A", "B", "C"])
        self.assertEqual(env, {"A": "override", "B": "two words"})

    def test_default_names(self):
        self.assertEqual(amux.default_name("/x/.envy", []), "envy")
        self.assertEqual(amux.default_name("/x/my repo", ["my-repo"]), "my-repo-2")
        self.assertEqual(amux.default_name("/", []), "session")

    def test_container_user(self):
        self.assertEqual(amux.container_user("jyxc-dz-0101541"), "jyxc-dz-0101541")
        self.assertEqual(amux.container_user("Chi"), "chi")
        self.assertEqual(amux.container_user("1x"), "amux")

    def test_image_tag_tracks_inputs(self):
        tag = amux.image_tag("/nix/store/a", {"V": "1"})
        self.assertTrue(tag.startswith("envy-amux:"))
        self.assertNotEqual(tag, amux.image_tag("/nix/store/a", {"V": "2"}))
        self.assertEqual(tag, amux.image_tag("/nix/store/a", {"V": "1"}))

    def test_parse_defaults_to_new(self):
        args = amux.parse(["codex", "--", "--resume"])
        self.assertEqual((args.command, args.agent, args.agent_args), ("new", "codex", ["--resume"]))
        args = amux.parse(["-s", "x", "--add-dir", "/tmp:rw"])
        self.assertEqual((args.command, args.s, args.add_dir, args.agent, args.agent_args),
                         ("new", "x", ["/tmp:rw"], "claude", []))
        self.assertFalse(args.no_history)
        self.assertTrue(amux.parse(["--no-history", "codex"]).no_history)

    def test_parse_passes_arguments_after_the_agent(self):
        args = amux.parse(["codex", "resume", "--last"])
        self.assertEqual((args.agent, args.agent_args), ("codex", ["resume", "--last"]))
        args = amux.parse(["new", "-s", "x", "-c", "/w", "claude", "-c", "-s", "y"])
        self.assertEqual((args.s, args.c, args.agent, args.agent_args), ("x", "/w", "claude", ["-c", "-s", "y"]))
        self.assertEqual(amux.parse(["codex", "exec", "--", "-x"]).agent_args, ["exec", "--", "-x"])
        self.assertEqual(amux.parse(["claude", "-h"]).agent_args, ["-h"])
        self.assertEqual(amux.parse(["a", "-t", "x"]).t, "x")
        self.assertEqual(amux.parse(["kill-session", "-t", "x"]).func, amux.cmd_kill)
        self.assertEqual(amux.parse(["ls", "-F", "#{session_name}"]).F, "#{session_name}")
        self.assertEqual(amux.parse(["update"]).func, amux.cmd_update)
        self.assertEqual(amux.parse(["gc"]).func, amux.cmd_gc)

    def test_format_session(self):
        session = amux.Session("a", "codex", "/w", "now")
        self.assertEqual(amux.format_session("#{session_name}:#{agent} #{workdir} #{nope}", session), "a:codex /w ")

    def test_tools_root_ignores_order(self):
        root = amux.tools_root(["/nix/store/a", "/nix/store/b"])
        self.assertTrue(root.startswith(amux.TOOLS_ROOTS + "/tools-"))
        self.assertEqual(root, amux.tools_root(["/nix/store/b", "/nix/store/a"]))
        self.assertNotEqual(root, amux.tools_root(["/nix/store/a"]))

    def test_gc_refuses_running_sessions(self):
        with mock.patch.object(amux, "list_sessions", return_value=[amux.Session("a", "claude", "/w", "now")]):
            with self.assertRaisesRegex(amux.AmuxError, "sessions are running"):
                amux.cmd_gc(amux.parse(["gc"]), {"nix": {"tools": []}})

    def test_stale_clients(self):
        listing = "/dev/pts/1 10:Mon_Oct_1\n/dev/pts/2 11:Mon_Oct_1\n/dev/pts/3 -\n"
        alive = lambda pid, start: pid == 10 and start == "Mon_Oct_1"
        self.assertEqual(amux.stale_clients(listing, alive), ["/dev/pts/2"])

    def test_next_session(self):
        sessions = [amux.Session(n, "claude", "/w", "now") for n in ("a", "b", "c")]
        self.assertEqual(amux.next_session(amux.EXIT_NEXT, "c", sessions, lambda: None), "a")
        self.assertEqual(amux.next_session(amux.EXIT_PREV, "a", sessions, lambda: None), "c")
        self.assertEqual(amux.next_session(amux.EXIT_CHOOSE, "a", sessions, lambda: "b"), "b")
        self.assertEqual(amux.next_session(amux.EXIT_CHOOSE, "a", sessions, lambda: None), "a")
        self.assertEqual(amux.next_session(amux.EXIT_NEXT, "gone", sessions, lambda: None), "c")
        self.assertIsNone(amux.next_session(amux.EXIT_NEXT, "a", [], lambda: None))



class AgentVersionTests(unittest.TestCase):
    REGISTRY = "https://npm.test/"
    PACKAGES = {
        "@anthropic-ai/claude-code-linux-arm64/latest": {
            "version": "2.0.0", "dist": {"tarball": "https://npm.test/c.tgz", "integrity": "sha512-c"}},
        "@openai/codex/latest": {"version": "0.9.0"},
        "@openai/codex/0.9.0-linux-arm64": {
            "version": "0.9.0-linux-arm64", "dist": {"tarball": "https://npm.test/x.tgz", "integrity": "sha512-x"}},
    }
    AGENTS = {"claude": {"version": "2.0.0", "url": "https://npm.test/c.tgz", "integrity": "sha512-c"},
              "codex": {"version": "0.9.0", "url": "https://npm.test/x.tgz", "integrity": "sha512-x"}}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "amux", "agents.json")
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def get(self, registry, path):
        self.calls.append(path)
        self.assertEqual(registry, self.REGISTRY)
        return self.PACKAGES[path]

    def offline(self, registry, path):
        raise OSError("offline")

    def resolve(self, get, now=1000.0, **kwargs):
        return amux.resolve_agents(self.cache, self.REGISTRY, "arm64", now=lambda: now, get=get, **kwargs)

    def test_latest_agents(self):
        self.assertEqual(amux.latest_agents(self.REGISTRY, "arm64", self.get), self.AGENTS)
        self.calls.clear()
        packages = {k.replace("arm64", "x64"): v for k, v in self.PACKAGES.items()}
        amux.latest_agents(self.REGISTRY, "amd64", lambda r, p: self.calls.append(p) or packages[p])
        self.assertIn("@openai/codex/0.9.0-linux-x64", self.calls)

    def test_cache_is_fresh_for_a_day(self):
        self.assertEqual(self.resolve(self.get), self.AGENTS)
        self.calls.clear()
        self.assertEqual(self.resolve(self.offline, now=1000.0 + amux.VERSION_TTL - 1), self.AGENTS)
        self.assertEqual(self.resolve(self.get, now=1000.0 + amux.VERSION_TTL), self.AGENTS)
        self.assertEqual(len(self.calls), 3)
        self.resolve(self.get, now=1000.0 + amux.VERSION_TTL, force=True)
        self.assertEqual(len(self.calls), 6)

    def test_offline_falls_back(self):
        with self.assertRaises(amux.AmuxError):
            self.resolve(self.offline)
        self.assertEqual(self.resolve(self.offline, fallback=lambda: self.AGENTS), self.AGENTS)
        self.resolve(self.get)
        stale = 1000.0 + 2 * amux.VERSION_TTL
        self.assertEqual(self.resolve(self.offline, now=stale, fallback=lambda: None), self.AGENTS)
        with self.assertRaises(amux.AmuxError):
            self.resolve(self.offline, now=stale, force=True)

    def test_cache_is_scoped_to_registry_and_arch(self):
        self.resolve(self.get)
        with self.assertRaises(amux.AmuxError):
            amux.resolve_agents(self.cache, self.REGISTRY, "amd64", get=self.offline)

    def test_build_args_carry_agent_packages(self):
        cfg = {"image": {"buildArgs": {"BASE_IMAGE": "b"}}, "user": "u", "home": "/h"}
        args = amux.build_args(cfg, self.AGENTS)
        self.assertEqual(args["CLAUDE_URL"], "https://npm.test/c.tgz")
        self.assertEqual(args["CODEX_INTEGRITY"], "sha512-x")
        self.assertEqual(args["BASE_IMAGE"], "b")


if __name__ == "__main__":
    unittest.main()
