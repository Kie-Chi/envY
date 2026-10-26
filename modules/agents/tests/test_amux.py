"""Unit checks for the amux host client: input filtering, mounts, and commands."""

import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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

    def plan(self, root, layout=None, add_dirs=(), skills=None, ssh=None):
        return amux.plan_mounts(
            root=root, home=self.home, layout=layout, add_dirs=list(add_dirs),
            skills=skills, paste_dir=os.path.join(self.home, ".cache/amux/paste"), ssh_agent=ssh,
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
        self.assertIn(amux.Mount("/nix/store", "/nix/store", readonly=True), mounts)
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

    def test_optional_skills_and_ssh_agent(self):
        work = os.path.join(self.home, "work")
        skills = os.path.join(self.home, ".claude/skills")
        os.makedirs(work)
        self.assertNotIn(skills, [m.target for m in self.plan(work, skills=skills)])
        os.makedirs(skills)
        mounts = self.plan(work, skills=skills, ssh="/run/host-services/ssh-auth.sock")
        self.assertIn(amux.Mount(skills, skills, readonly=True), mounts)
        self.assertIn(amux.Mount("/run/host-services/ssh-auth.sock", amux.SSH_AGENT_MOUNT), mounts)

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
        self.assertEqual((args.command, args.s, args.add_dir, args.agent), ("new", "x", ["/tmp:rw"], "claude"))
        self.assertEqual(amux.parse(["a", "-t", "x"]).t, "x")
        self.assertEqual(amux.parse(["kill-session", "-t", "x"]).func, amux.cmd_kill)

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


if __name__ == "__main__":
    unittest.main()
