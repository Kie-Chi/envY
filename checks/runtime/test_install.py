"""Fake-only install probe tests; never boot a guest or invoke a real installer."""

import contextlib
from dataclasses import asdict, replace
import hashlib
from types import SimpleNamespace
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tarfile
import time
import unittest
from unittest.mock import patch

# unittest discovery and direct file execution both resolve the sibling driver.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import install
import test_vm

vm = install.vm

INSTALL_FAKE = test_vm.FAKE_TOOL
INSTALL_FAKE = INSTALL_FAKE[:INSTALL_FAKE.index("receipt = {'token': token")]+r'''
receipt = {'token': token, 'arch': 'x86_64', 'version': '2.33.1', 'initialNoNix': True,
           'daemon': True, 'daemonService': True, 'buildUsers': 32, 'profile': True,
           'installerExitZero': True, 'error': None,
           'bootstrapTools': {tool: True for tool in TOOLS},
           'stages': {stage: True for stage in STAGES}}
if mode == 'wrong-token': receipt['token'] = 'wrong'
if mode == 'wrong-version': receipt['version'] = '2.32.0'
if mode == 'wrong-arch': receipt['arch'] = 'aarch64'
if mode == 'daemon': receipt['daemon'] = False
if mode == 'daemon-zero': receipt['daemon'] = 1
if mode == 'buildgroup': receipt['buildUsers'] = 0
if mode == 'buildbool': receipt['buildUsers'] = True
if mode == 'missing-install': del receipt['stages']['install']
if mode == 'failed-install': receipt['stages']['install'] = False
if mode == 'exit-nonzero': receipt['installerExitZero'] = False
if mode == 'initial-nix': receipt['initialNoNix'] = False
if mode == 'profile': receipt['profile'] = False
if mode == 'service': receipt['daemonService'] = False
if mode == 'missing-tool': receipt['bootstrapTools']['sudo'] = False
if mode == 'optional-tools':
    receipt['bootstrapTools']['curl'] = False
    receipt['bootstrapTools']['git'] = False
if mode == 'guest-error': receipt['error'] = 'failed:install'
print('ENVY_RUNTIME_INSTALL_RECEIPT ' + json.dumps(receipt), flush=True)
if mode == 'duplicate': print('ENVY_RUNTIME_INSTALL_RECEIPT ' + json.dumps(receipt))
if mode == 'receipt-error': sys.exit(9)
'''
INSTALL_FAKE = "STAGES = " + repr(install.STAGES) + "\nTOOLS = " + repr(install.TOOLS) + "\n" + INSTALL_FAKE


class InstallProbeTests(unittest.TestCase):
    configure = test_vm.BootProbeTests.configure
    calls = test_vm.BootProbeTests.calls
    assert_base_unchanged = test_vm.BootProbeTests.assert_base_unchanged
    assert_query_spawn = test_vm.BootProbeTests.assert_query_spawn
    assert_owned_stopped = test_vm.BootProbeTests.assert_owned_stopped
    timeout_after_handshake = test_vm.BootProbeTests.timeout_after_handshake
    replace_base_parent = test_vm.BootProbeTests.replace_base_parent

    probe = staticmethod(install.install_probe)
    runner_class = install.InstallRunner
    assert_canary = test_vm.BootProbeTests.assert_canary
    test_regular_seed_graft_rebinding_fails_without_exporting_foreign_body = test_vm.BootProbeTests.test_regular_seed_graft_rebinding_fails_without_exporting_foreign_body
    test_seed_leaf_replacement_before_qemu_never_reads_foreign_body = test_vm.BootProbeTests.test_seed_leaf_replacement_before_qemu_never_reads_foreign_body
    test_iso_readonly_binding_rejects_foreign_regular_before_body_read = test_vm.BootProbeTests.test_iso_readonly_binding_rejects_foreign_regular_before_body_read
    test_mkdir_first_open_foreign_directory_is_untrusted = test_vm.BootProbeTests.test_mkdir_first_open_foreign_directory_is_untrusted
    test_native_stages_keep_overlay_and_seed_fds_after_leaf_replacement = test_vm.BootProbeTests.test_native_stages_keep_overlay_and_seed_fds_after_leaf_replacement
    test_log_creation_then_path_chmod_cannot_change_foreign_mode = test_vm.BootProbeTests.test_log_creation_then_path_chmod_cannot_change_foreign_mode

    def setUp(self):
        test_vm.BootProbeTests.setUp(self)
        self.tar = self.root / 'nix.tar.xz'
        self.tar_bytes = b'public synthetic archive bytes; not an installer\n'
        self.tar.write_bytes(self.tar_bytes)
        self.tar.chmod(0o444)
        self.tar_sha = hashlib.sha256(self.tar_bytes).hexdigest()
        self.config = install.InstallConfig(**asdict(self.config), nix_tar=str(self.tar), install_timeout=1)
        for name in ('qemu', 'qemu-img', 'mkisofs'):
            (self.tools / name).write_text('#!' + sys.executable + '\n' + INSTALL_FAKE)
        for name, value in (('NIX_TAR_SHA256', self.tar_sha), ('NIX_TAR_SIZE', len(self.tar_bytes))):
            patched = patch.object(install, name, value)
            patched.start()
            self.addCleanup(patched.stop)

    def assert_inputs_unchanged(self):
        self.assert_base_unchanged()
        self.assertEqual(self.tar.read_bytes(), self.tar_bytes)
        self.assertEqual(stat.S_IMODE(self.tar.stat().st_mode), 0o444)

    def test_success_is_install_only_with_closed_seed_and_qemu_arguments(self):
        before = Path.cwd()
        with patch.object(vm.os, 'environ', {'SYNTHETIC_SECRET': 'must-not-inherit',
                                                  'ENVY_RUNTIME_DARWIN_PS': test_vm.TEST_DARWIN_PS}):
            result = install.install_probe(self.config)
        self.assertEqual(Path.cwd(), before)
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['evidence'], 'linux-vm-nix-install-only')
        for key in ('envyInstall', 'envyConfig', 'envyApply', 'requiresMissingNixHttpsBranch', 'rollbackClaimed'):
            self.assertIs(result[key], False)
        self.assertEqual(stat.S_IMODE((self.root / 'owned').stat().st_mode), 0o700)
        calls = self.calls()
        self.assertEqual([c['role'] for c in calls], ['qemu-img'] * 3 + ['mkisofs', 'qemu'])
        argv = calls[-1]['args']
        for option, value in (('-nic', 'none'), ('-display', 'none'), ('-monitor', 'none'),
                              ('-machine', 'pc,accel=tcg'), ('-serial', 'stdio')):
            self.assertEqual(argv[argv.index(option) + 1], value)
        for forbidden in ('-drive', '-netdev', '-net', '-virtfs', '-fsdev', '-qmp', '-chardev', '-vnc'):
            self.assertNotIn(forbidden, argv)
        nodes = [json.loads(argv[i + 1]) for i, a in enumerate(argv) if a == '-blockdev']
        self.assertTrue(nodes[0]['backing']['read-only'])
        self.assertEqual(nodes[0]['backing']['file']['filename'], '/dev/fdset/1')
        self.assertTrue(nodes[1]['read-only'])
        self.assertEqual(nodes[1]['file']['filename'], '/dev/fdset/3')
        self.assertEqual(nodes[0]['file']['filename'], '/dev/fdset/2')
        observed = json.loads((self.tools / 'observations/seed-observed.json').read_text())
        self.assertEqual(observed['nix.tar.xz'], self.tar_sha)
        for call in calls:
            self.assertNotIn('SYNTHETIC_SECRET', call['env'])
            self.assertEqual(call['env']['HOME'], str(self.tools))
            self.assertEqual(call['env']['TMPDIR'], vm.native_tmp())
            self.assertEqual(call['cwd'], str(self.tools))
        seed = (self.tools / 'observations/user-data').read_text()
        for fragment in ('envy-runtime', '/home/envy-runtime', 'NOPASSWD:ALL', '--daemon', '--yes',
                         '--no-channel-add', 'store', 'daemon-rpc', self.tar_sha):
            self.assertIn(fragment, seed)
        for forbidden in (str(self.root), 'ssh_authorized_keys', 'requires.sh', 'envy apply', 'hostfwd'):
            self.assertNotIn(forbidden, seed)
        self.assertNotIn('guest-script', seed)
        self.assertFalse(list((self.root / 'owned').glob('.seed-*')))
        self.assert_inputs_unchanged()

    def test_bad_tar_paths_size_hash_and_parameters_never_spawn(self):
        linked = self.root / 'linked'
        linked.symlink_to(self.tar)
        mutations = [
            {'nix_tar': None}, {'nix_tar': str(self.root / 'missing')}, {'nix_tar': str(self.root)},
            {'nix_tar': str(linked)}, {'nix_tar': 'relative'}, {'nix_tar': str(self.tar) + ',x'},
            {'nix_tar': str(self.tar) + '\nx'}, {'nix_tar': str(self.tar) + ':x'},
            {'workdir': str(self.root / 'bad,option')}, {'workdir': str(self.root / 'bad\noption')},
            {'sha256': '0' * 64}, {'qemu': str(self.root / 'missing-tool')},
            {'install_timeout': 0}, {'install_timeout': True}, {'install_timeout': 1201},
            {'install_timeout': float('nan')}, {'install_timeout': float('inf')},
            {'timeout': float('nan')}, {'timeout': 601}, {'output_limit': 0},
        ]
        with patch.object(vm.subprocess, 'Popen') as spawn:
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    result = install.install_probe(replace(self.config, **mutation))
                    self.assertEqual(result['status'], 'unavailable', result)
                    self.assertEqual(result['commands'], [])
                    self.assertFalse((self.root / 'owned').exists())
            for data, mode in ((b'', 0o444), (b'wrong-size', 0o444), (b'x' * len(self.tar_bytes), 0o444),
                               (self.tar_bytes, 0o644)):
                self.tar.chmod(0o644)
                self.tar.write_bytes(data)
                self.tar.chmod(mode)
                result = install.install_probe(self.config)
                self.assertEqual(result['status'], 'unavailable', result)
                self.assertFalse((self.root / 'owned').exists())
            spawn.assert_not_called()
        self.assert_base_unchanged()

    def test_qemu_exit_zero_and_incomplete_receipts_never_pass(self):
        modes = ('none', 'wrong-token', 'wrong-version', 'wrong-arch', 'daemon', 'daemon-zero',
                 'buildgroup', 'buildbool', 'missing-install', 'failed-install', 'exit-nonzero',
                 'initial-nix', 'profile', 'service', 'missing-tool', 'guest-error', 'duplicate',
                 'receipt-error', 'exit-error')
        for index, mode in enumerate(modes):
            with self.subTest(mode=mode):
                self.configure(mode=mode)
                cfg = replace(self.config, workdir=str(self.root / ('case' + str(index))))
                result = install.install_probe(cfg)
                self.assertFalse(result['ok'])
                self.assertEqual(result['status'], 'failure')
                self.assertIsNone(result['receipt'])
                self.assertTrue(Path(result['commands'][-1]['stdout']).is_file())
                self.assertNotIn('synthetic private diagnostic', json.dumps(result))
        self.assert_inputs_unchanged()

    def test_optional_bootstrap_tools_can_be_absent_but_are_reported(self):
        self.configure(mode='optional-tools')
        result = install.install_probe(self.config)
        self.assertTrue(result['ok'], result)
        self.assertIs(result['receipt']['bootstrapTools']['curl'], False)
        self.assertIs(result['receipt']['bootstrapTools']['git'], False)

    def test_tar_parent_rename_keeps_hashed_inode_through_payload_copy(self):
        directory = self.root / 'archive-dir'
        directory.mkdir()
        self.tar.rename(directory / 'tar')
        self.tar = directory / 'tar'
        self.config = replace(self.config, nix_tar=str(self.tar))
        original = install.verified_tar
        held = []

        @contextlib.contextmanager
        def swapped(config, deadline):
            with original(config, deadline) as source:
                held.append(source.fileno())
                directory.rename(self.root / 'verified-archive')
                directory.mkdir()
                (directory / 'tar').write_bytes(b'foreign bytes must not reach mkisofs')
                yield source

        with patch.object(install, 'verified_tar', swapped):
            result = install.install_probe(self.config)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.tools / 'observations/nix.tar.xz').read_bytes(), self.tar_bytes)
        with self.assertRaises(OSError):
            os.fstat(held[0])
        self.assert_base_unchanged()

    def test_base_parent_rename_keeps_verified_fd(self):
        original = vm.preflight
        verified = self.base.stat()

        @contextlib.contextmanager
        def swapped(config, deadline):
            with original(config, deadline) as prepared:
                self.replace_base_parent()
                yield prepared

        with patch.object(vm, 'preflight', swapped):
            result = install.install_probe(self.config)
        self.assertTrue(result['ok'], result)
        for call in self.calls():
            if 'base' in call:
                self.assertEqual(call['base']['inode'], verified.st_ino)
                self.assertEqual(call['base']['sha256'], self.config.sha256)

    def test_owned_workdir_rename_and_foreign_leaf_cannot_redirect_native_reads_or_writes(self):
        original = install.InstallRunner.run
        retained = self.root / 'retained'
        work = self.root / 'owned'
        foreign = self.root / 'foreign'
        foreign.write_bytes(b'foreign data must never be exported')
        before = Path.cwd()

        def swap(runner, stage, argv, **kwargs):
            if stage == 'seed-create':
                work.rename(retained)
                work.mkdir()
                for leaf in ('user-data', 'meta-data', 'network-config', 'nix.tar.xz', 'seed.iso'):
                    (work / leaf).symlink_to(foreign)
            return original(runner, stage, argv, **kwargs)

        with patch.object(install.InstallRunner, 'run', swap):
            result = install.install_probe(self.config)
        self.assertEqual(Path.cwd(), before)
        self.assertTrue(result['ok'], result)
        self.assertEqual((self.tools / 'observations/nix.tar.xz').read_bytes(), self.tar_bytes)
        self.assertEqual(foreign.read_bytes(), b'foreign data must never be exported')
        self.assertEqual(len(list(work.iterdir())), 5)
        self.assertTrue((retained / 'qemu.stdout.log').is_file())
        self.assert_inputs_unchanged()

    def test_stage_leaf_replacement_is_rejected_before_copy_and_native_read(self):
        original = os.open
        foreign = self.root / 'foreign'
        foreign.write_bytes(b'foreign')
        swapped = []

        def replace_leaf(path, flags, *args, **kwargs):
            if isinstance(path, str) and path.startswith('.seed-') and not flags & os.O_CREAT:
                directory = kwargs['dir_fd']
                os.unlink(path, dir_fd=directory)
                os.symlink(str(foreign), path, dir_fd=directory)
                swapped.append(path)
            return original(path, flags, *args, **kwargs)

        with patch.object(install.os, 'open', side_effect=replace_leaf):
            result = install.install_probe(self.config)
        self.assertFalse(result['ok'])
        self.assertTrue(swapped)
        self.assertEqual(len(result['commands']), 3)
        self.assertEqual(foreign.read_bytes(), b'foreign')
        self.assert_inputs_unchanged()

    def test_descriptor_allowlist_closes_after_success_and_cancel(self):
        original = vm.subprocess.Popen
        def checked_spawn(*args, **kwargs):
            if args[0][0] == self.darwin_ps:
                self.assert_query_spawn(args[0], kwargs)
            for fd in kwargs['pass_fds']:
                self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
            return original(*args, **kwargs)
        for index, mode in enumerate(('good', 'cancel')):
            self.configure(mode=mode)
            with patch.object(vm.subprocess, 'Popen', side_effect=checked_spawn) as spawn:
                result = install.install_probe(replace(self.config, workdir=str(self.root / ('fds' + str(index)))))
            self.assertEqual(result['ok'], mode == 'good', result)
            all_fds = set()
            for call in spawn.call_args_list:
                self.assertTrue(call.kwargs['close_fds'])
                all_fds.update(call.kwargs['pass_fds'])
            native_calls = [call for call in spawn.call_args_list if call.args[0][0] != self.darwin_ps]
            self.assertEqual(len(native_calls[3].kwargs['pass_fds']), 5)
            for fd in all_fds:
                with self.assertRaises(OSError):
                    os.fstat(fd)
        self.assert_inputs_unchanged()

    def test_timeout_cancel_and_log_cap_reap_only_owned_group(self):
        unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)
        try:
            for index, (mode, expected) in enumerate((('hang', 'timeout'), ('cancel', 'cancelled'), ('flood', 'output-limit'))):
                self.configure(mode=mode)
                original = install.InstallRunner.run

                def shorten(runner, stage, argv, **kwargs):
                    if stage == 'qemu':
                        runner.deadline = time.monotonic() + 0.4
                    return original(runner, stage, argv, **kwargs)

                started = time.monotonic()
                with (self.timeout_after_handshake(module=vm) if mode == 'hang' else contextlib.nullcontext()), \
                        patch.object(install.InstallRunner, 'run', shorten):
                    result = install.install_probe(replace(self.config, workdir=str(self.root / ('stop' + str(index))),
                                                           output_limit=4096))
                self.assertEqual(result['commands'][-1]['stage'], 'qemu', result)
                self.assertEqual(result['error'], expected, result)
                self.assertLess(time.monotonic() - started, 5)
                self.assertIsNone(os.waitid(os.P_PID, unrelated.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
                self.assert_owned_stopped(result)
                logs = list(Path(result['workdir']).glob('*.log'))
                self.assertLessEqual(sum(p.stat().st_size for p in logs), 4096)
                self.assertTrue((Path(result['workdir']) / 'overlay.qcow2').exists())
            self.assert_inputs_unchanged()
        finally:
            lease = vm._ProcessLease(unrelated, vm.wait_flags())
            try:
                vm.Runner.stop(lease)
            finally:
                lease.reap()

    def test_preparation_cancel_and_hash_cancel_close_owned_handles(self):
        for stage in ('info', 'create', 'resize'):
            self.configure(prepare_cancel=stage)
            result = install.install_probe(replace(self.config, workdir=str(self.root / stage)))
            self.assertEqual(result['error'], 'cancelled', result)
            self.assert_owned_stopped(result)
        with patch.object(install.hashlib, 'sha256', side_effect=KeyboardInterrupt):
            result = install.install_probe(self.config)
        self.assertEqual(result['error'], 'cancelled')
        self.assertFalse((self.root / 'owned').exists())
        self.assert_inputs_unchanged()

    def test_cli_cancel_is_injected_after_handshake_and_handlers_are_restored(self):
        self.configure(mode='cancel')
        args = ['install-probe', '--workdir', self.config.workdir,
                '--base', self.config.base, '--sha256', self.config.sha256,
                '--nix-tar', self.config.nix_tar, '--qemu', self.config.qemu,
                '--qemu-img', self.config.qemu_img, '--mkisofs', self.config.mkisofs]
        if test_vm.TEST_DARWIN_PS is not None:
            args.extend(["--darwin-ps", test_vm.TEST_DARWIN_PS])
        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(install.main(args), 1)
        result = json.loads(output.getvalue())
        self.assertEqual(result['error'], 'cancelled', result)
        self.assert_owned_stopped(result)
        self.assertEqual(handlers, {s: signal.getsignal(s) for s in handlers})
        self.assert_inputs_unchanged()

    def test_cli_rejects_shell_and_missing_arguments_and_restores_signals(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        for args in ([], ['boot-probe'], ['install-probe'],
                     ['install-probe', '--workdir', self.config.workdir],
                     ['install-probe', '--workdir', self.config.workdir, '--guest-script', 'id'],
                     ['install-probe', '--workdir', self.config.workdir, '--nix-tar-sha256', '0' * 64]):
            with self.subTest(args=args), patch.object(vm.subprocess, 'Popen') as spawn:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = install.main(args)
                self.assertEqual(code, 1)
                self.assertFalse(json.loads(output.getvalue())['ok'])
                spawn.assert_not_called()
        self.assertEqual(handlers, {s: signal.getsignal(s) for s in handlers})


class GuestArchiveTests(unittest.TestCase):
    def setUp(self):
        self.guest = {'__name__': 'synthetic_guest_test'}
        exec(compile(install.guest_program('a' * 64, 1200), '<synthetic-guest>', 'exec'), self.guest)
        self.prefix = self.guest['ARCHIVE_PREFIX']

    def member(self, suffix, kind=tarfile.REGTYPE, link=''):
        item = tarfile.TarInfo(self.prefix + '/' + suffix)
        item.type, item.linkname = kind, link
        return item

    def test_archive_validation_allows_official_shape_and_absolute_closure_symlinks(self):
        closure = '0' * 32 + '-public'
        entries = [self.member('install'), self.member('store', tarfile.DIRTYPE),
                   self.member('store/' + closure, tarfile.DIRTYPE), self.member('store/' + closure + '/target'),
                   self.member('store/' + closure + '/relative', tarfile.SYMTYPE, 'target'),
                   self.member('store/' + closure + '/absolute', tarfile.SYMTYPE, '/nix/store/' + closure + '/target')]
        self.guest['validate_members'](entries)

    def test_archive_rejects_traversal_devices_hardlinks_and_link_parents(self):
        cases = [
            [tarfile.TarInfo('/etc/foreign')], [tarfile.TarInfo('../foreign')],
            [self.member('../foreign')], [self.member('a/../../foreign')], [self.member('a\nforeign')],
            [self.member('device', tarfile.CHRTYPE)], [self.member('fifo', tarfile.FIFOTYPE)],
            [self.member('hard', tarfile.LNKTYPE, self.prefix + '/install')],
            [self.member('link', tarfile.SYMTYPE, '/etc/passwd')],
            [self.member('link', tarfile.SYMTYPE, '../../foreign')],
            [self.member('link', tarfile.SYMTYPE, 'missing')],
            [self.member('link', tarfile.SYMTYPE, 'install'), self.member('link/foreign')],
            [self.member('install')],
        ]
        for entries in cases:
            with self.subTest(entries=[e.name for e in entries]):
                with self.assertRaises(self.guest['GuestFailure']):
                    self.guest['validate_members']([self.member('install'), *entries])

    def run_user_probe(self, *, missing_tool=None, initial_nix=False, failure=None, bad_hash=False):
        """Execute the real closed guest steps against synthetic host boundaries."""
        data = b'synthetic fixed public archive'
        commands = []
        archive = unittest.mock.MagicMock()
        archive.__enter__.return_value = archive
        archive.getmembers.return_value = [self.member('install')]
        with test_vm.tempfile.TemporaryDirectory(prefix='envy-install-guest-unit-') as temporary:
            path = Path(temporary) / 'public.tar'
            path.write_bytes(data)
            original_open = os.open

            def open_source(name, flags, *args, **kwargs):
                self.assertEqual(name, '/run/envy-runtime-install-seed/nix.tar.xz')
                self.assertEqual(flags, os.O_RDONLY | os.O_NOFOLLOW)
                return original_open(path, flags)

            def command(argv, timeout=30, echo=False):
                commands.append((argv, timeout, echo))
                if argv[0] == '/bin/bash' and '--daemon' in argv:
                    if failure == 'install':
                        raise self.guest['GuestFailure']('synthetic installer failed')
                    return b''
                if argv[-1] == '--version':
                    return b'nix (Nix) 2.33.1\n' if failure != 'version' else b'nix (Nix) 2.32.0\n'
                if 'info' in argv:
                    if failure == 'rpc':
                        raise self.guest['GuestFailure']('synthetic daemon unavailable')
                    return json.dumps({'url': 'daemon' if failure != 'local-store' else 'local',
                                       'version': '2.33.1'}).encode()
                if 'is-active' in argv:
                    return b'active\n' if failure != 'service' else b'inactive\n'
                if argv[0] == '/bin/bash':
                    return self.guest['NIX'].encode() if failure != 'profile' else b'/wrong/bin/nix'
                raise AssertionError(argv)

            self.guest.update(EXPECTED_SHA256='0' * 64 if bad_hash else hashlib.sha256(data).hexdigest(),
                              EXPECTED_SIZE=len(data), command=command)
            output = io.StringIO()
            with contextlib.ExitStack() as stack:
                stack.enter_context(patch.object(os, 'getuid', return_value=1000))
                stack.enter_context(patch.object(self.guest['platform'], 'machine', return_value='x86_64'))
                stack.enter_context(patch.object(os, 'open', side_effect=open_source))
                stack.enter_context(patch.object(os.path, 'lexists', return_value=initial_nix))
                stack.enter_context(patch.object(self.guest['pwd'], 'getpwuid',
                                                 return_value=SimpleNamespace(pw_name='envy-runtime', pw_dir='/home/envy-runtime')))
                stack.enter_context(patch.object(self.guest['pwd'], 'getpwall', return_value=[] if failure == 'build-users' else
                                                 [SimpleNamespace(pw_name='nixbld1', pw_uid=30001, pw_gid=30000)]))
                stack.enter_context(patch.object(self.guest['grp'], 'getgrnam',
                                                 return_value=SimpleNamespace(gr_gid=30000, gr_mem=[])))
                stack.enter_context(patch.object(self.guest['shutil'], 'which', side_effect=lambda tool, **kw:
                                                 None if tool in ('nix', missing_tool) else '/usr/bin/' + tool))
                stack.enter_context(patch.object(tarfile, 'open', return_value=archive))
                stack.enter_context(patch.object(self.guest['tempfile'], 'mkdtemp', return_value='/tmp/synthetic-extraction'))
                stack.enter_context(contextlib.redirect_stdout(output))
                self.guest['user_probe']()
            raw = output.getvalue().encode()
        receipt = json.loads(raw.decode().split(install.RECEIPT_PREFIX)[1])
        return raw, receipt, commands, archive

    def test_closed_guest_success_runs_official_installer_then_explicit_daemon_rpc(self):
        raw, receipt, commands, archive = self.run_user_probe()
        self.assertIsNone(receipt['error'])
        self.assertEqual(install.receipt_from(raw, 'a' * 64), receipt)
        self.assertEqual(commands[0], (['/bin/bash', '/tmp/synthetic-extraction/' + self.prefix + '/install',
                                        '--daemon', '--yes', '--no-channel-add'], 1200, True))
        self.assertEqual(commands[2][0], [self.guest['NIX'], '--extra-experimental-features', 'nix-command',
                                          '--store', 'daemon', 'store', 'info', '--json'])
        self.assertEqual(archive.extractall.call_args.kwargs['filter'], 'fully_trusted')

    def test_guest_missing_required_tool_initial_nix_or_bad_hash_never_installs(self):
        for kwargs, stage in (({'missing_tool': 'sudo'}, 'bootstrap'),
                              ({'initial_nix': True}, 'initial-no-nix'), ({'bad_hash': True}, 'archive-hash')):
            with self.subTest(kwargs=kwargs):
                raw, receipt, commands, archive = self.run_user_probe(**kwargs)
                self.assertEqual(receipt['error'], 'failed:' + stage)
                self.assertEqual(commands, [])
                archive.extractall.assert_not_called()
                with self.assertRaises(vm.ProbeError):
                    install.receipt_from(raw, 'a' * 64)

    def test_guest_install_rpc_service_buildusers_profile_failures_remain_failures(self):
        for failure in ('install', 'version', 'rpc', 'local-store', 'service', 'build-users', 'profile'):
            with self.subTest(failure=failure):
                raw, receipt, commands, archive = self.run_user_probe(failure=failure)
                self.assertIsNotNone(receipt['error'])
                with self.assertRaises(vm.ProbeError):
                    install.receipt_from(raw, 'a' * 64)
                self.assertEqual(receipt['installerExitZero'], failure != 'install')
                self.assertEqual(len(commands) >= 1, True)

    def test_guest_installer_is_unprivileged_and_uses_fixed_commands_and_budget(self):
        program = install.guest_program('a' * 64, 1200)
        compile(program, '<guest>', 'exec')
        for fragment in ('"--daemon", "--yes", "--no-channel-add"', 'timeout=INSTALL_TIMEOUT',
                         '"--store", "daemon", "store", "info", "--json"', 'filter="fully_trusted"',
                         '"/usr/bin/sudo", "-n", "-u", "envy-runtime"', '"initialNoNix"',
                         'start_new_session=True', 'process.wait(timeout=3)', '"nix-daemon.service"'):
            self.assertIn(fragment, program)
        for fragment in ('shell=True', 'curl -', 'wget ', 'envy apply', 'requires.sh', 'os.environ', 'rmtree'):
            self.assertNotIn(fragment, program)


if __name__ == '__main__':
    unittest.main()
