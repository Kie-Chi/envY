import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(os.environ.get("ENVY_TEST_ROOT", Path(__file__).resolve().parents[3]))
ZSH = shutil.which("zsh")


@unittest.skipUnless(ZSH, "Provide zsh for isolated shell function checks")
class ShellFunctionTests(unittest.TestCase):
    def run_shell(self, body, available=(), completion=None):
        with tempfile.TemporaryDirectory(prefix="envy-functions-") as directory:
            home = Path(directory)
            if completion is not None:
                (home / "_systemctl").write_text(completion)
            stubs = "\n".join(
                name + '() { print -r -- "' + name + ':$*"; }'
                for name in available
            )
            platform = "darwin" if "launchctl" in available else "linux-gnu"
            script = '\n'.join([
                "OSTYPE=" + platform, 'compdef() { :; }', stubs, 'source "$1"',
                'sudo() { "$@"; }', body,
            ])
            environment = {
                "HOME": directory, "ZDOTDIR": directory, "PATH": directory,
                "ENVY_TEST_COMPLETIONS": directory, "TERM": "xterm-256color",
            }
            return subprocess.run(
                [ZSH, "-dfc", script, "fixture", str(ROOT / "files/zsh/func.zsh")],
                env=environment, cwd=home, text=True, capture_output=True, timeout=5,
            )

    def assert_shell(self, body, expected, **kwargs):
        result = self.run_shell(body, **kwargs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        self.assertEqual(result.stdout.splitlines(), expected)

    def test_missing_systemctl_completion_returns_without_an_autoload_error(self):
        for registered in (False, True):
            with self.subTest(registered=registered):
                self.assert_shell(
                    'fpath=("$ENVY_TEST_COMPLETIONS")\n'
                    + ('autoload -Uz _systemctl\n' if registered else '')
                    + 'words=(sc st ""); CURRENT=3\n'
                    '_sc_comp; print -r -- "completion=$?"',
                    ['completion=1'], available=('systemctl',),
                )

    def test_service_completion_uses_the_execution_argument_expansion(self):
        cases = [
            ('sc', 'st', 'systemctl status target.service', 3),
            ('sc', 'go', 'systemctl start target.service', 3),
            ('sc', 'enn', 'systemctl enable --now target.service', 4),
            ('usc', 'disn', 'systemctl --user disable --now target.service', 5),
            ('sc', 'act', 'systemctl list-units --type=service --state=active target.service', 5),
        ]
        for command, action, words, cursor in cases:
            with self.subTest(command=command, action=action):
                self.assert_shell(
                    '_systemctl() { print -r -- "$words|$CURRENT"; }\n'
                    f'words=({command} {action} target.service); CURRENT=3\n'
                    '_sc_comp\n'
                    'print -r -- "after=$words|$CURRENT"',
                    [words + '|' + str(cursor), f'after={command} {action} target.service|3'],
                    available=('systemctl',),
                )

    def test_service_completion_loads_a_real_fpath_function(self):
        self.assert_shell(
            'fpath=("$ENVY_TEST_COMPLETIONS")\n'
            'autoload -Uz _systemctl\n'
            'words=(usc st target.service); CURRENT=3\n_sc_comp',
            ['systemctl --user status target.service'],
            available=('systemctl',), completion='print -r -- "$words"\n',
        )

    def test_service_wrappers_preserve_arguments_and_do_not_leak_scratch_arrays(self):
        self.assert_shell(
            '_SC_ARGS=(original)\nsc enn "space name.service"\n'
            'usc act\nsc\nusc\nprint -r -- "scratch=$_SC_ARGS"',
            ['systemctl:enable --now space name.service',
             'systemctl:--user list-units --type=service --state=active',
             'systemctl:', 'systemctl:--user', 'scratch=original'],
            available=('systemctl',),
        )

    def test_service_commands_are_not_defined_without_a_platform_backend(self):
        self.assert_shell('print -r -- "$+functions[sc] $+functions[usc]"', ['0 0'])

    def test_package_completion_selects_the_same_backend_as_execution(self):
        self.assert_shell(
            '_brew() { print -r -- "completion:$words|$CURRENT"; }\n'
            '_apt() { print WRONG_BACKEND; }\n'
            'pm i package\nwords=(pm i package); CURRENT=3\n_pm',
            ['brew:install package', 'completion:brew install package|3'],
            available=('brew', 'apt'),
        )

    def test_package_completion_accepts_the_execution_aliases(self):
        for action in ('i', 'in', 'install'):
            with self.subTest(action=action):
                self.assert_shell(
                    '_apt() { print -r -- "$words"; }\n'
                    f'words=(pm {action} package); CURRENT=3\n_pm',
                    ['apt install package'], available=('apt',),
                )

    def test_missing_package_completion_returns_without_an_autoload_error(self):
        self.assert_shell(
            'fpath=("$ENVY_TEST_COMPLETIONS")\n'
            'words=(pm i package); CURRENT=3\n_pm; print -r -- "completion=$?"',
            ['completion=1'], available=('brew',),
        )

    def test_package_clean_executes_each_backend_sequence_without_eval(self):
        for backend, expected in (
            ('brew', ['brew:cleanup']),
            ('apt', ['apt:autoremove', 'apt:clean']),
            ('dnf', ['dnf:autoremove', 'dnf:clean all']),
            ('pacman', ['pacman:-Sc']),
        ):
            with self.subTest(backend=backend):
                self.assert_shell('pm cl', expected, available=(backend,))

    def test_package_actions_preserve_backend_arguments_and_privileges(self):
        actions = ('i package', 's package', 'rm package', 'up ignored', 'ug ignored')
        cases = {
            'brew': ['brew:install package', 'brew:search package',
                     'brew:uninstall package', 'brew:update', 'brew:upgrade'],
            'apt': ['sudo:apt install package', 'apt:search package',
                    'sudo:apt remove package', 'sudo:apt update', 'sudo:apt upgrade'],
            'dnf': ['sudo:dnf install package', 'dnf:search package',
                    'sudo:dnf remove package', 'sudo:dnf check-update', 'sudo:dnf upgrade'],
            'pacman': ['sudo:pacman -S package', 'pacman:-Ss package',
                       'sudo:pacman -Rs package', 'sudo:pacman -Sy', 'sudo:pacman -Syu'],
        }
        for backend, expected in cases.items():
            with self.subTest(backend=backend):
                self.assert_shell(
                    'sudo() { print -r -- "sudo:$*"; }\n'
                    + '\n'.join('pm ' + action for action in actions),
                    expected, available=(backend,),
                )

    def test_package_arguments_stay_quoted_and_failed_cleanup_does_not_continue(self):
        self.assert_shell(
            'brew() { print -r -- "$#:$2"; }\npm i "two words"',
            ['2:two words'], available=('brew',),
        )
        self.assert_shell(
            'apt() { print -r -- "apt:$*"; return 7; }\n'
            'pm cl; print -r -- "status=$?"',
            ['apt:autoremove', 'status=7'], available=('apt',),
        )

    def test_darwin_service_domain_and_log_wrappers_are_preserved(self):
        self.assert_shell(
            'id() { print 501; }\n'
            'sc st org.example.service\nusc res org.example.agent\n'
            'jlog ExampleApp',
            ['launchctl:print system/org.example.service',
             'launchctl:kickstart -k gui/501/org.example.agent',
             "log:show --last 1h --predicate process == 'ExampleApp' --info"],
            available=('launchctl', 'log'),
        )

    def test_linux_log_wrappers_keep_system_and_user_arguments(self):
        self.assert_shell(
            'jlog example.service -f\nujlog -f',
            ['journalctl:-xe -u example.service -f', 'journalctl:--user -xe -f'],
            available=('journalctl',),
        )

    def test_utility_entrypoints_and_aliases_survive_the_loader(self):
        self.assert_shell(
            'for name in copypath copyfile mkcd mypy extract cat sudo pm _envy; do\n'
            '  print -r -- "$name=$+functions[$name]"\ndone\n'
            "alias cpd cpf ec c",
            ['copypath=1', 'copyfile=1', 'mkcd=1', 'mypy=1', 'extract=1',
             'cat=1', 'sudo=1', 'pm=1', '_envy=1',
             'cpd=copypath', 'cpf=copyfile', 'ec=extract', 'c=cat'],
        )


if __name__ == "__main__":
    unittest.main()
