"""Full host orchestration tests with temporary synthetic inputs and fake tools."""
import contextlib
import copy
from dataclasses import asdict, replace
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import full
import test_acceptance as fixtures
import test_install as install_fixtures
import test_vm as vm_fixtures

vm, install, acceptance = full.vm, full.install, full.acceptance

FULL_FAKE = vm_fixtures.FAKE_TOOL
FULL_FAKE = FULL_FAKE[:FULL_FAKE.index("receipt = {'token': token")] + r'''
metadata = json.loads(seed['acceptance.json'])
assert 'metadata.json' not in seed and set(seed) == {'user-data', 'meta-data', 'network-config', 'nix.tar.xz', 'source.tar', 'acceptance.json'}
assert seed['acceptance.json'] == json.dumps(metadata, sort_keys=True) + '\n'
if mode.startswith('abort-'):
    frame = {'schemaVersion': 1, 'token': token, 'error': 'file-changed'}
    if mode in ('abort-wrong-token', 'abort-then-success'): frame['token'] = '0' * 64
    if mode == 'abort-open-code': frame['error'] = 'arbitrary-safe-looking-code'
    if mode == 'abort-bool': frame['schemaVersion'] = True
    if mode == 'abort-extra': frame['extra'] = 'private'
    if mode == 'abort-nonfinite': frame['schemaVersion'] = float('nan')
    raw = json.dumps(frame)
    if mode == 'abort-duplicate': raw = raw[:-1] + ', "error": "guest-platform"}'
    if mode == 'abort-broken': raw = '{broken'
    if mode == 'abort-overlong': raw += ' ' * 4096
    line = '[ 33.05] cloud-init[151]: ENVY_RUNTIME_GUEST_ABORT ' + raw
    if mode != 'abort-partial': line += '\n'
    if mode in ('abort-good', 'abort-stderr'):
        child = start_child()
    target = 2 if mode == 'abort-stderr' else 1
    encoded = line.encode()
    for part in (encoded[:45], encoded[45:77], encoded[77:]):
        os.write(target, part)
        time.sleep(0.02)
    if mode == 'abort-then-success':
        pass  # A foreign nonce must not stop the later valid success receipt.
    else:
        if mode not in ('abort-good', 'abort-stderr'):
            child = start_child()
        time.sleep(60)
row = settings['receipt']
row['token'] = token
row['source'] = metadata['source']
installed = row['install']
installed['token'] = token
if mode == 'wrong-token': row['token'] = 'wrong'
if mode == 'wrong-source': row['source'] = {**row['source'], 'manifestSha256': '0' * 64}
if mode == 'wrong-mode': row['transport'] = 'nix-export'
if mode == 'wrong-evidence': row['evidence'] = 'linux-vm-nix-install-only'
if mode == 'wrong-version': installed['version'] = '0.0'
if mode == 'bad-secret': row['stages']['firstApply']['secrets']['ageKeyMode0600'] = False
if mode == 'missing-stage': del row['stages']['repeatApply']
if mode == 'sequence': row['stages']['repeatApply']['journal']['operationId'] = row['stages']['firstApply']['journal']['operationId']
if mode == 'drift': row['stages']['recoveryApply']['managedSha256'] = '0' * 64
if mode == 'partial':
    del row['stages']['recoveryApply']
    row['stages']['faultApply']['partialReadableAfterRecovery'] = False
    row['error'] = 'failed-or-unknown:recoveryApply'
if mode == 'raw-secret': row['stages']['firstApply']['rawSecret'] = 'SYNTHETIC-PRIVATE-DO-NOT-ECHO'
print('ENVY_RUNTIME_INSTALL_RECEIPT ' + json.dumps(installed), flush=True)
print('ENVY_RUNTIME_ACCEPTANCE_RECEIPT ' + json.dumps(row), flush=True)
if mode == 'duplicate': print('ENVY_RUNTIME_ACCEPTANCE_RECEIPT ' + json.dumps(row), flush=True)
if mode == 'invalid': print('ENVY_RUNTIME_ACCEPTANCE_RECEIPT {broken', flush=True)
if mode in ('receipt-error', 'partial', 'raw-secret'):
    sys.stderr.write('SYNTHETIC-PRIVATE-DO-NOT-ECHO')
    sys.exit(9)
'''


class AbortLinesTests(unittest.TestCase):
    TOKEN = 'a' * 64

    def frame(self, **changes):
        return (full.guest.ABORT_PREFIX + json.dumps(
            {'schemaVersion': 1, 'token': self.TOKEN, 'error': 'file-changed', **changes})).encode()

    def test_bytewise_split_and_decorated_crlf_requires_newline(self):
        scanner = full.AbortLines(self.TOKEN)
        for byte in b'[33.1] cloud-init[151]: ' + self.frame() + b'\r':
            self.assertIsNone(scanner.feed(bytes([byte])))
            self.assertLessEqual(len(scanner.pending), 4096)
        self.assertEqual(scanner.feed(b'\n'), 'file-changed')

    def test_long_lines_are_discarded_until_newline_then_valid_abort_is_seen(self):
        scanner = full.AbortLines(self.TOKEN)
        for _ in range(2000):
            self.assertIsNone(scanner.feed(b'x' * 8192))
            self.assertLessEqual(len(scanner.pending), 4096)
        self.assertIsNone(scanner.feed(self.frame() + b'\n'))
        self.assertEqual(scanner.feed(self.frame(error='unknown') + b'\n'), 'unknown')

    def test_strict_json_rejects_nonfinite_duplicate_trailing_and_open_codes(self):
        valid = self.frame()
        cases = [valid + b' trailing', valid + b' {}', valid.replace(b'1,', b'true,'), valid.replace(b'1,', b'1.0,'),
                 valid.replace(b'1,', b'NaN,'), valid.replace(b'1,', b'Infinity,'),
                 valid[:-1] + b', "error": "guest-platform"}',
                 valid[:-1] + b', "token": "' + self.TOKEN.encode() + b'"}',
                 self.frame(error='a' * 101), self.frame(error='private-value'),
                 self.frame(error=['file-changed']), self.frame(error=None),
                 self.frame(extra='unexpected'), self.frame(token='b' * 64),
                 b'ENVY_RUNTIME_GUEST_ABORT ' + b'[' * 1500 + b']' * 1500,
                 self.frame().replace(b'file-changed', b'\xff')]
        for raw in cases:
            with self.subTest(raw=raw[:80]):
                scanner = full.AbortLines(self.TOKEN)
                self.assertIsNone(scanner.feed(raw + b'\n'))
                self.assertEqual(scanner.feed(valid + b'\n'), 'file-changed')

    def test_closed_error_catalog_is_short_static_and_every_code_is_accepted(self):
        for code in full.guest.ABORT_CODES:
            self.assertRegex(code, r'^[a-z0-9:-]{1,100}$')
            self.assertEqual(full.AbortLines(self.TOKEN).feed(self.frame(error=code) + b'\n'), code)


class FullRunnerLeaseTests(unittest.TestCase):
    def setUp(self):
        # Share the controlled Popen/selector fixture, never real PID signals.
        self.fixture = vm_fixtures.RunnerLeaseTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.runner = full.FullRunner(self.fixture.root, [str(self.fixture.root / 'fake')],
            time.monotonic() + 10, 65536, self.fixture.fd, self.fixture.root, darwin_ps=vm_fixtures.TEST_DARWIN_PS)

    def run_fake(self):
        return self.runner.run_qemu(['fake'], (), 'a' * 64)

    def test_closed_stdio_normal_exit_stops_before_reap(self):
        self.assertEqual(self.run_fake(), b'')
        self.assertLess(self.fixture.events.index('stop'), self.fixture.events.index('wait'))
        self.fixture.process.poll.assert_not_called()
        self.assertEqual(self.fixture.events.count('stop'), 1)
        self.assertEqual(self.runner.commands[-1]['returncode'], 0)
        self.fixture.selector.close.assert_called_once()
        self.fixture.process.stdout.close.assert_called_once()
        self.fixture.process.stderr.close.assert_called_once()

    def test_echild_revokes_signals_and_never_reports_zero(self):
        self.fixture.observations = [ChildProcessError()]
        with self.assertRaises(ChildProcessError):
            self.run_fake()
        self.fixture.kill.assert_not_called()
        self.assertIsNone(self.runner.commands[-1]['returncode'])
        self.fixture.process.wait.assert_called_once()

    def test_cleanup_preserves_first_error_and_closes_every_resource(self):
        first = OSError('first-stop-error')
        self.fixture.kill.side_effect = first
        self.fixture.process.wait.side_effect = OSError('later-wait-error')
        self.fixture.process.stdout.close.side_effect = OSError('later-close-error')
        with self.assertRaises(OSError) as caught:
            self.run_fake()
        self.assertIs(caught.exception, first)
        self.fixture.process.wait.assert_called_once()
        self.fixture.process.stdout.close.assert_called_once()
        self.fixture.process.stderr.close.assert_called_once()
        self.fixture.selector.close.assert_called_once()

    def test_early_returncode_revokes_signal_authority(self):
        self.fixture.process.returncode = 0
        with self.assertRaisesRegex(vm.ProbeError, 'process-lease-lost'):
            self.run_fake()
        self.fixture.kill.assert_not_called()
        self.assertIsNone(self.runner.commands[-1]['returncode'])

    def test_log_context_exit_attempts_all_closes_and_keeps_first_error(self):
        original = vm.exclusive_file
        first = OSError('first-full-context-exit')
        held = []
        @contextlib.contextmanager
        def failing_exit(path, **kwargs):
            try:
                with original(path, **kwargs) as stream:
                    held.append(stream.fileno())
                    yield stream
            finally:
                if path.endswith('.stderr.log'):
                    raise first
                raise OSError('later-full-context-exit')
        with patch.object(vm, 'exclusive_file', failing_exit):
            with self.assertRaises(OSError) as caught:
                self.run_fake()
        self.assertIs(caught.exception, first)
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_cleanup_error_is_not_hidden_by_callers_handled_exception(self):
        first = OSError('first-full-stop-error')
        self.fixture.kill.side_effect = first
        try:
            raise ValueError('callers-handled-error')
        except ValueError:
            with self.assertRaises(OSError) as caught:
                self.run_fake()
        self.assertIs(caught.exception, first)
        self.fixture.process.wait.assert_called_once()
        self.fixture.process.stdout.close.assert_called_once()
        self.fixture.process.stderr.close.assert_called_once()

    def test_primary_cancel_survives_cleanup_failure(self):
        self.fixture.observations = [None]
        self.fixture.selector.get_map.return_value = {'open': True}
        self.fixture.selector.select.side_effect = KeyboardInterrupt
        self.fixture.kill.side_effect = OSError('cleanup-stop-error')
        with self.assertRaises(KeyboardInterrupt):
            self.run_fake()
        self.fixture.process.wait.assert_called_once()
        self.fixture.process.stdout.close.assert_called_once()
        self.fixture.process.stderr.close.assert_called_once()


class FullProbeTests(unittest.TestCase):
    configure_base = vm_fixtures.BootProbeTests.configure
    calls = vm_fixtures.BootProbeTests.calls
    assert_base_unchanged = vm_fixtures.BootProbeTests.assert_base_unchanged
    assert_query_spawn = vm_fixtures.BootProbeTests.assert_query_spawn
    assert_owned_stopped = vm_fixtures.BootProbeTests.assert_owned_stopped
    timeout_after_handshake = vm_fixtures.BootProbeTests.timeout_after_handshake

    def configure(self, **settings):
        self.configure_base(receipt=self.receipt, **settings)

    def setUp(self):
        # Bootstrap the existing base/tools fixture without a receipt dependency.
        self.receipt = {}
        install_fixtures.InstallProbeTests.setUp(self)
        receipt = fixtures.ReceiptTests()
        receipt.setUp()
        self.receipt = copy.deepcopy(receipt.row)
        self.receipt.update(transport='guest-fetch', evidence=acceptance.NETWORK_EVIDENCE)
        self.receipt['stages']['faultApply']['exitCode'] = 97
        self.source = self.root / 'snapshot'
        self.source.mkdir()
        entries = []
        for name, data, mode in (('flake.lock', b'{"nodes":{"root":{}}}\n', 0o644),
                                  ('install.sh', b'#!/bin/sh\nexit 0\n', 0o755)):
            (self.source / name).write_bytes(data)
            (self.source / name).chmod(mode)
            entries.append(acceptance.snapshot._entry(name, data, mode, 'working-tree'))
        manifest = {'schemaVersion': 1, 'files': sorted(entries, key=lambda row: row['relativePath']),
                    'lockSha256': hashlib.sha256((self.source / 'flake.lock').read_bytes()).hexdigest(),
                    'fixture': {'selectionStage': 'minimal', 'secrets': 'guest-generated-only'}}
        (self.source / acceptance.snapshot.MANIFEST_NAME).write_bytes(acceptance.snapshot._manifest_bytes(manifest))
        (self.source / acceptance.snapshot.MANIFEST_NAME).chmod(0o600)
        info = self.source.stat()
        self.config = full.NetworkConfig(**asdict(self.config), snapshot=str(self.source),
            snapshot_identity=(info.st_dev, info.st_ino), source_store_path=fixtures.store('source'), full_timeout=10)
        self.baseline = {p.name: (p.read_bytes(), p.stat().st_mode) for p in self.source.iterdir()}
        for name in ('qemu', 'qemu-img', 'mkisofs'):
            (self.tools / name).write_text('#!' + sys.executable + '\n' + FULL_FAKE)
        self.configure()
        # Every orchestration subprocess must be one of our temporary fakes.
        original = subprocess.Popen

        def guarded(argv, *args, **kwargs):
            if argv[0] == self.darwin_ps:
                self.assert_query_spawn(argv, kwargs)
                return original(argv, *args, **kwargs)
            self.assertIn(argv[0], [str(self.tools / name) for name in ('qemu', 'qemu-img', 'mkisofs')])
            return original(argv, *args, **kwargs)

        self.spawn = patch.object(subprocess, 'Popen', side_effect=guarded)
        self.spawn_mock = self.spawn.start()
        self.addCleanup(self.spawn.stop)

    def unchanged(self):
        self.assert_base_unchanged()
        self.assertEqual(self.tar.read_bytes(), self.tar_bytes)
        self.assertEqual(self.tar.stat().st_mode & 0o777, 0o444)
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mode) for p in self.source.iterdir()}, self.baseline)

    def probe(self, **changes):
        return full.full_probe(replace(self.config, **changes))

    def test_decorated_split_abort_reaps_owned_group_before_full_deadline(self):
        for index, mode in enumerate(('abort-good', 'abort-stderr')):
            self.configure(mode=mode)
            started = time.monotonic()
            with patch.object(vm.os, 'killpg', wraps=os.killpg) as killpg:
                report = self.probe(workdir=str(self.root / ('abort' + str(index))), full_timeout=5)
            self.assertEqual(killpg.call_args_list, [unittest.mock.call(c['pid'], signal.SIGKILL)
                                                       for c in report['commands']])
            self.assertLess(time.monotonic() - started, 3)
            self.assertEqual(report['error'], 'guest-entrypoint-failed:file-changed', report)
            self.assertFalse(report['ok'])
            self.assertFalse(report['fullReady'])
            self.assertIsNone(report['evidence'])
            self.assertIsNone(report['receipt'])
            self.assertIsNone(report['partial'])
            self.assert_owned_stopped(report)
            log = 'stderr' if mode == 'abort-stderr' else 'stdout'
            self.assertIn('cloud-init[151]: ENVY_RUNTIME_GUEST_ABORT ',
                          Path(report['commands'][-1][log]).read_text())
        self.unchanged()

    def test_invalid_unbound_or_unterminated_abort_cannot_stop_as_guest_failure(self):
        original = full.FullRunner.run_qemu
        def bounded(runner, *args):
            runner.deadline = min(runner.deadline, time.monotonic() + 0.35)
            return original(runner, *args)
        for index, mode in enumerate(('abort-wrong-token', 'abort-duplicate', 'abort-partial',
                'abort-open-code', 'abort-bool', 'abort-extra', 'abort-nonfinite', 'abort-broken', 'abort-overlong')):
            self.configure(mode=mode)
            with self.subTest(mode=mode), self.timeout_after_handshake(module=full), patch.object(full.FullRunner, 'run_qemu', bounded):
                report = self.probe(workdir=str(self.root / ('invalid-abort' + str(index))))
                self.assertEqual(report['error'], 'timeout', report)
                self.assertFalse(report['ok'])
                self.assertIsNone(report['partial'])
                self.assert_owned_stopped(report)
        self.unchanged()

    def test_foreign_abort_does_not_preempt_original_complete_success(self):
        self.configure(mode='abort-then-success')
        report = self.probe()
        self.assertTrue(report['ok'], report)
        self.assertIsNone(report['error'])
        self.assertIsNotNone(report['receipt'])
        self.unchanged()

    def test_closed_seed_network_metadata_bytes_and_complete_receipt(self):
        before = Path.cwd()
        with patch.dict(os.environ, {'SYNTHETIC_HOST_SECRET': 'DO-NOT-INHERIT'}):
            report = self.probe()
        self.assertTrue(report['ok'], report)
        self.assertEqual(report['status'], 'passed')
        self.assertEqual(report['evidence'], acceptance.NETWORK_EVIDENCE)
        self.assertTrue(report['fullReady'])
        self.assertFalse(report['rollbackClaimed'])
        self.assertIsNone(report['partial'])
        self.assertEqual(Path.cwd(), before)
        self.assertEqual([c['stage'] for c in report['commands']],
                         ['image-info', 'overlay-create', 'overlay-resize', 'seed-create', 'qemu'])
        calls = self.calls()
        self.assertEqual(len(calls), 5)
        argv = calls[-1]['args']
        self.assertEqual(argv.count('-nic'), 1)
        self.assertEqual(argv[argv.index('-nic') + 1], full.NIC)
        for forbidden in ('hostfwd', 'guestfwd', 'tftp', 'smb', '-virtfs', '-fsdev', '-netdev', '-qmp', '-drive'):
            self.assertNotIn(forbidden, ' '.join(argv))
        for call in calls:
            self.assertNotIn('SYNTHETIC_HOST_SECRET', call['env'])
            self.assertEqual(call['env']['HOME'], str(self.tools))
            self.assertEqual(call['env']['XDG_CONFIG_HOME'], str(self.tools))
            self.assertEqual(call['env']['XDG_CACHE_HOME'], str(self.tools))
            self.assertEqual(call['cwd'], str(self.tools))
            self.assertEqual(call['env']['TMPDIR'], vm.native_tmp())
        observed = self.tools / 'observations'
        self.assertEqual((observed / 'network-config').read_bytes(), full.NETWORK_CONFIG)
        data = (observed / 'acceptance.json').read_bytes()
        metadata = json.loads(data)
        self.assertEqual(data, (json.dumps(metadata, sort_keys=True) + '\n').encode())
        self.assertEqual(metadata['source'], report['receipt']['source'])
        user_data = (observed / 'user-data').read_text()
        self.assertIn(hashlib.sha256(data).hexdigest(), user_data)
        self.assertIn('existingNixBranch', user_data)
        self.assertEqual((observed / 'nix.tar.xz').read_bytes(), self.tar_bytes)
        self.assertNotIn(str(self.root), user_data)
        self.assertFalse(list(Path(report['workdir']).glob('.seed-*')))
        self.unchanged()

    def test_native_fd_capabilities_and_payload_context_live_through_qemu(self):
        original_spawn = self.spawn_mock.side_effect
        held, stages = [], []
        def checked(argv, *args, **kwargs):
            if argv[0] == self.darwin_ps:
                return original_spawn(argv, *args, **kwargs)
            fds = kwargs['pass_fds']
            self.assertTrue(kwargs['close_fds'])
            self.assertTrue(kwargs['start_new_session'])
            self.assertIs(kwargs['shell'], False)
            self.assertTrue(all(stat.S_ISREG(os.fstat(fd).st_mode) for fd in fds))
            held.extend(fds)
            roles = []
            for fd in fds:
                roles.append(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE)
            stages.append((Path(argv[0]).name, roles))
            if Path(argv[0]).name == 'mkisofs':
                self.assertEqual(roles, [os.O_RDWR] + [os.O_RDONLY] * 6)
                self.assertEqual(int(argv[argv.index('-output') + 1].rsplit('/', 1)[1]), fds[0])
                grafts = argv[argv.index('-graft-points') + 1:]
                self.assertEqual([g.split('=')[0] for g in grafts],
                    ['user-data', 'meta-data', 'network-config', 'nix.tar.xz', 'source.tar', 'acceptance.json'])
                self.assertEqual([int(g.rsplit('/', 1)[1]) for g in grafts], list(fds[1:]))
                self.assertTrue(all(os.fstat(fd).st_nlink == 0 for fd in fds[1:]))
                self.assertEqual(argv[argv.index('-iso-level') + 1], '3')
            if Path(argv[0]).name == 'qemu':
                self.assertEqual(roles, [os.O_RDONLY, os.O_RDWR, os.O_RDONLY])
                self.assertTrue(all(os.fstat(fd).st_nlink == 0 for fd in payload_fds))
            return original_spawn(argv, *args, **kwargs)
        original_payload = acceptance.approved_network_payloads
        payload_fds = []
        @contextlib.contextmanager
        def tracked(*args):
            with original_payload(*args) as approved:
                payload_fds.extend(approved['pass_fds'])
                yield approved
        with patch.object(acceptance, 'approved_network_payloads', tracked):
            self.spawn_mock.side_effect = checked
            report = self.probe()
        self.assertTrue(report['ok'], report)
        self.assertEqual(stages[:3], [('qemu-img', [os.O_RDONLY]),
                                     ('qemu-img', [os.O_RDONLY, os.O_RDWR]),
                                     ('qemu-img', [os.O_RDONLY, os.O_RDWR])])
        for fd in set(held + payload_fds):
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.unchanged()

    def test_bad_configuration_has_zero_io_mutations_and_spawns(self):
        mutations = [{'full_timeout': x} for x in (True, 0, 7201, float('nan'), float('inf'))]
        mutations += [{'install_timeout': 1201}, {'timeout': 601}, {'output_limit': True},
                      {'snapshot_identity': (1.0, 2)}, {'snapshot_identity': (True, 2)},
                      {'snapshot_identity': None}, {'snapshot': '/tmp/x,option'}, {'source_store_path': fixtures.store('other')},
                      {'source_store_path': None}, {'workdir': '/tmp/x\noption'}, {'base': None},
                      {'sha256': 'wrong'}, {'nix_tar': None}, {'qemu': '/tmp/tool,option'},
                      {'preparation_receipt': '/tmp/preparation'}, {'export_artifact': '/tmp/export'}]
        with patch.object(os, 'open', side_effect=AssertionError('IO before validation')), \
             patch.object(Path, 'mkdir', side_effect=AssertionError('mutation before validation')):
            for changes in mutations:
                with self.subTest(changes=changes):
                    report = self.probe(**changes)
                    self.assertFalse(report['ok'])
                    self.assertEqual(report['status'], 'unavailable')
                    self.assertEqual(report['commands'], [])
        self.spawn_mock.assert_not_called()
        self.unchanged()

    def test_missing_mismatched_invalid_and_bad_stage_frames_never_pass(self):
        for index, mode in enumerate(('none', 'wrong-token', 'wrong-source', 'wrong-mode', 'wrong-evidence',
            'wrong-version', 'bad-secret', 'missing-stage', 'sequence', 'drift', 'duplicate', 'invalid', 'exit-error')):
            with self.subTest(mode=mode):
                self.configure(mode=mode)
                report = self.probe(workdir=str(self.root / ('case' + str(index))))
                self.assertFalse(report['ok'], report)
                self.assertFalse(report['fullReady'])
                self.assertIsNone(report['evidence'])
                self.assertIsNone(report['receipt'])
                if mode in ('wrong-token', 'wrong-source', 'wrong-mode', 'wrong-evidence', 'duplicate', 'invalid'):
                    self.assertIsNone(report['partial'])
                self.assertEqual(report['status'], 'failure')
                self.assertTrue(Path(report['commands'][-1]['stdout']).is_file())
        self.unchanged()

    def test_nonzero_and_partial_preserve_only_safe_observed_facts_and_logs(self):
        for index, mode in enumerate(('receipt-error', 'partial', 'raw-secret')):
            with self.subTest(mode=mode):
                self.configure(mode=mode)
                report = self.probe(workdir=str(self.root / ('partial' + str(index))))
                self.assertEqual(report['error'], 'tool-failed:qemu')
                self.assertFalse(report['ok'])
                self.assertIsNone(report['evidence'])
                self.assertIsNone(report['receipt'])
                self.assertFalse(report['rollbackClaimed'])
                self.assertTrue(report['partial']['installValidated'])
                self.assertEqual(report['partial']['stages']['faultApply']['journal']['terminal'], 'partial')
                self.assertEqual(report['partial']['stages']['faultApply']['exitCode'], 97)
                self.assertNotIn('SYNTHETIC-PRIVATE', json.dumps(report))
                self.assertIn('SYNTHETIC-PRIVATE', Path(report['commands'][-1]['stderr']).read_text())
                if mode == 'partial':
                    self.assertNotIn('recoveryApply', report['partial']['stages'])
        self.unchanged()

    def test_single_deadline_covers_preparation_boot_install_and_post_context(self):
        deadlines = []
        original_run = install.InstallRunner.run
        original_qemu = full.FullRunner.run_qemu
        original_tar = install.verified_tar
        original_payload = acceptance.approved_network_payloads
        @contextlib.contextmanager
        def tar(config, deadline):
            deadlines.append(deadline)
            with original_tar(config, deadline) as stream:
                yield stream
        @contextlib.contextmanager
        def payload(*args):
            deadlines.append(args[2])
            with original_payload(*args) as prepared:
                yield prepared
        def run(runner, stage, argv, **kwargs):
            deadlines.append(runner.deadline)
            return original_run(runner, stage, argv, **kwargs)
        def qemu(runner, *args):
            deadlines.append(runner.deadline)
            return original_qemu(runner, *args)
        started = time.monotonic()
        with patch.object(install, 'verified_tar', tar), \
             patch.object(acceptance, 'approved_network_payloads', payload), \
             patch.object(install.InstallRunner, 'run', run), patch.object(full.FullRunner, 'run_qemu', qemu):
            report = self.probe(full_timeout=8)
        self.assertTrue(report['ok'], report)
        self.assertEqual(len(set(deadlines)), 1)
        self.assertGreaterEqual(deadlines[0], started + 8)
        self.assertLess(deadlines[0], started + 8.5)
        # Expiration during preparation must never grant a fresh QEMU budget.
        def expired(runner, stage, argv, **kwargs):
            result = original_run(runner, stage, argv, **kwargs)
            if stage == 'seed-create':
                clock.return_value = runner.deadline + 1
            return result
        with patch.object(full.time, 'monotonic', return_value=time.monotonic()) as clock, \
             patch.object(install.InstallRunner, 'run', expired):
            report = self.probe(workdir=str(self.root / 'expired'))
        self.assertEqual(report['error'], 'timeout')
        self.assertEqual(len(report['commands']), 4)
        self.unchanged()

    def test_post_payload_binding_failure_never_promotes_valid_receipt(self):
        original = acceptance.approved_network_payloads
        @contextlib.contextmanager
        def fail_after(*args):
            with original(*args) as approved:
                yield approved
                raise vm.ProbeError('snapshot-binding-changed')
        with patch.object(acceptance, 'approved_network_payloads', fail_after):
            report = self.probe()
        self.assertFalse(report['ok'])
        self.assertIsNone(report['receipt'])
        self.assertIsNone(report['evidence'])
        self.assertEqual(report['error'], 'snapshot-binding-changed')

    def test_network_upgrade_requires_exactly_one_fixed_offline_nic(self):
        original = vm.qemu_argv
        for transform in (lambda argv: argv + ['-nic', 'none'],
                          lambda argv: [full.NIC if a == 'none' else a for a in argv],
                          lambda argv: [a for a in argv if a != '-nic']):
            with self.subTest(transform=transform), patch.object(vm, 'qemu_argv', side_effect=lambda *a: transform(original(*a))):
                with self.assertRaises(vm.ProbeError):
                    full.network_argv('/fixed/qemu', 5, 6, 7)

    def test_bound_qemu_logs_survive_namespace_replacement_without_trusting_foreign_bytes(self):
        original_qemu = full.FullRunner.run_qemu
        retained = self.root / 'retained'
        work = Path(self.config.workdir)
        def rename(runner, *args):
            work.rename(retained)
            work.mkdir()
            (work / 'qemu.stdout.log').write_text('SYNTHETIC-FOREIGN-RECEIPT')
            return original_qemu(runner, *args)
        self.configure(mode='partial')
        with patch.object(full.FullRunner, 'run_qemu', rename):
            report = self.probe()
        self.assertFalse(report['ok'])
        self.assertTrue(report['partial']['installValidated'])
        self.assertEqual((work / 'qemu.stdout.log').read_text(), 'SYNTHETIC-FOREIGN-RECEIPT')
        self.assertIn(acceptance.RECEIPT_PREFIX, (retained / 'qemu.stdout.log').read_text())
        self.unchanged()

    def test_timeouts_flood_and_cancel_reap_only_owned_processes_and_close_fds(self):
        original_qemu = full.FullRunner.run_qemu
        def bounded(runner, *args):
            runner.deadline = min(runner.deadline, time.monotonic() + 0.5)
            return original_qemu(runner, *args)
        for index, (mode, reason) in enumerate((('hang', 'timeout'), ('flood', 'output-limit'), ('cancel', 'cancelled'))):
            self.configure(mode=mode)
            with (self.timeout_after_handshake(module=full) if mode == 'hang' else contextlib.nullcontext()), \
                    patch.object(full.FullRunner, 'run_qemu', bounded):
                report = self.probe(workdir=str(self.root / ('stop' + str(index))), output_limit=4096)
            self.assertEqual(report['commands'][-1]['stage'], 'qemu', report)
            self.assertEqual(report['error'], reason, report)
            self.assertFalse(report['ok'])
            self.assertIsNone(report['evidence'])
            self.assert_owned_stopped(report)
            self.assertLessEqual(sum(p.stat().st_size for p in Path(report['workdir']).glob('*.log')), 4096)
        self.unchanged()

    def test_invalid_immutable_inputs_cause_no_workdir_or_process(self):
        for changes in ({'sha256': '0' * 64}, {'nix_tar': str(self.root / 'missing')},
                        {'qemu_img': str(self.root / 'missing-tool')}):
            with self.subTest(changes=changes):
                report = self.probe(**changes)
                self.assertFalse(report['ok'])
                self.assertEqual(report['commands'], [])
                self.assertFalse(Path(self.config.workdir).exists())
        self.spawn_mock.assert_not_called()
        self.unchanged()

    def test_image_info_rejects_external_backing_encryption_or_wrong_format(self):
        for index, info in enumerate(({'format': 'raw', 'virtual-size': 4096},
              {'format': 'qcow2', 'virtual-size': 4096, 'backing-filename': '/foreign'},
              {'format': 'qcow2', 'virtual-size': 4096, 'encrypted': True},
              {'format': 'qcow2', 'virtual-size': True})):
            self.configure(info=info)
            report = self.probe(workdir=str(self.root / ('info' + str(index))))
            self.assertFalse(report['ok'])
            self.assertEqual([c['stage'] for c in report['commands']], ['image-info'])
        self.unchanged()

    def test_first_open_of_replaced_directory_does_not_claim_ownership_or_chmod(self):
        original = Path.mkdir
        work = Path(self.config.workdir)
        def swapped(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == work:
                path.rename(self.root / 'discarded')
                original(path)
                path.chmod(0o750)
                (path / 'overlay.qcow2').write_bytes(b'foreign-canary')
                (path / 'overlay.qcow2').chmod(0o640)
            return result
        with patch.object(Path, 'mkdir', swapped):
            report = self.probe()
        self.assertFalse(report['ok'])
        self.assertEqual(report['commands'], [])
        self.assertEqual(work.stat().st_mode & 0o777, 0o750)
        self.assertEqual((work / 'overlay.qcow2').stat().st_mode & 0o777, 0o640)
        self.assertEqual((work / 'overlay.qcow2').read_bytes(), b'foreign-canary')
        self.unchanged()

    def test_output_leaf_replacement_keeps_native_bound_fds_and_foreign_canaries(self):
        original_run = install.InstallRunner.run
        original_qemu = full.FullRunner.run_qemu
        work = Path(self.config.workdir)
        foreign_overlay, foreign_iso = self.root / 'foreign-overlay', self.root / 'foreign-iso'
        for path in (foreign_overlay, foreign_iso):
            path.write_bytes(b'foreign-canary-' + path.name.encode())
            path.chmod(0o640)
        def replace_leaf(leaf, foreign):
            path = work / leaf
            path.rename(work / ('original-' + leaf))
            path.symlink_to(foreign)
        def run(runner, stage, argv, **kwargs):
            if stage == 'overlay-resize':
                replace_leaf('overlay.qcow2', foreign_overlay)
            return original_run(runner, stage, argv, **kwargs)
        def qemu(runner, *args):
            replace_leaf('seed.iso', foreign_iso)
            return original_qemu(runner, *args)
        with patch.object(install.InstallRunner, 'run', run), patch.object(full.FullRunner, 'run_qemu', qemu):
            report = self.probe()
        self.assertTrue(report['ok'], report)
        for path in (foreign_overlay, foreign_iso):
            body = b'foreign-canary-' + path.name.encode()
            self.assertEqual(path.read_bytes(), body)
            self.assertEqual(path.stat().st_mode & 0o777, 0o640)
            for call in self.calls():
                self.assertNotIn(hashlib.sha256(body).hexdigest(), [row['sha256'] for row in call['reads']])
        self.unchanged()

    def test_preparation_cancel_cleans_owned_groups_and_handles_without_replay(self):
        for stage in ('info', 'create', 'resize'):
            self.configure(prepare_cancel=stage)
            previous = len(self.spawn_mock.call_args_list)
            report = self.probe(workdir=str(self.root / stage))
            self.assertEqual(report['error'], 'cancelled', report)
            self.assertFalse(report['ok'])
            self.assertFalse(report['rollbackClaimed'])
            self.assert_owned_stopped(report)
            self.assertNotIn('qemu', [c['stage'] for c in report['commands']])
            for call in self.spawn_mock.call_args_list[previous:]:
                for fd in call.kwargs['pass_fds']:
                    with self.assertRaises(OSError):
                        os.fstat(fd)
        self.unchanged()

    def cli_args(self, config=None):
        config = config or self.config
        args = ['full-probe']
        for name in ('workdir', 'snapshot', 'source_store_path', 'base', 'sha256', 'qemu', 'qemu_img', 'mkisofs', 'nix_tar'):
            args.extend(['--' + name.replace('_', '-'), getattr(config, name)])
        if config.darwin_ps is not None:
            args.extend(['--darwin-ps', config.darwin_ps])
        args.extend(['--snapshot-device', str(config.snapshot_identity[0]),
                     '--snapshot-inode', str(config.snapshot_identity[1]), '--full-timeout', '10'])
        return args

    def test_cli_rejects_invalid_arguments_without_effects_and_restores_handlers(self):
        handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
        for args in ([], ['full-probe'], self.cli_args() + ['--full-timeout', '7201'],
                     self.cli_args() + ['--install-timeout', '1201'],
                     self.cli_args() + ['--snapshot-inode', '1.0'],
                     self.cli_args() + ['--network', 'hostfwd=tcp::22-:22'],
                     self.cli_args() + ['--guest-command', 'id']):
            with self.subTest(args=args), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(full.main(args), 1)
                report = json.loads(output.getvalue())
                self.assertFalse(report['ok'])
                self.assertEqual(report['commands'], [])
        self.spawn_mock.assert_not_called()
        self.assertEqual(handlers, {s: signal.getsignal(s) for s in handlers})
        self.assertFalse(Path(self.config.workdir).exists())

    def test_cli_physical_identity_stays_python_integer_above_javascript_precision(self):
        identity = (2**60 + 17, 2**61 + 33)
        with patch.object(full, 'full_probe', return_value=full.empty_report()) as probe, \
             contextlib.redirect_stdout(io.StringIO()):
            full.main(self.cli_args(replace(self.config, snapshot_identity=identity)))
        self.assertEqual(probe.call_args.args[0].snapshot_identity, identity)
        self.assertTrue(all(type(v) is int for v in probe.call_args.args[0].snapshot_identity))

    def test_actual_cli_full_path_with_fake_tools_outputs_single_json(self):
        # Only fixed test archive constants are replaced. The CLI, orchestration,
        # seed renderer, payload boundary and receipt consumer all run unchanged.
        self.spawn.stop()
        bootstrap = ('import sys; sys.path.insert(0, ' + repr(str(Path(full.__file__).parent)) + '); '
                     'import full, install; install.NIX_TAR_SHA256=' + repr(self.tar_sha) + '; '
                     'install.NIX_TAR_SIZE=' + str(len(self.tar_bytes)) + '; sys.exit(full.main(sys.argv[1:]))')
        fixture_env = {'PATH': os.defpath, 'PYTHONDONTWRITEBYTECODE': '1'}
        if vm_fixtures.TEST_DARWIN_PS is not None:
            fixture_env['ENVY_RUNTIME_DARWIN_PS'] = vm_fixtures.TEST_DARWIN_PS
        result = subprocess.run([sys.executable, '-c', bootstrap, *self.cli_args()],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            env=fixture_env)
        self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
        report = json.loads(result.stdout)
        self.assertTrue(report['ok'], report)
        self.assertEqual(len(result.stdout.splitlines()), 1)
        self.assertEqual(result.stderr, b'')
        self.assertEqual(report['evidence'], acceptance.NETWORK_EVIDENCE)
        self.unchanged()


if __name__ == '__main__':
    unittest.main()
