"""Offline boot-driver tests: fake tools and synthetic readonly disks only."""

import ast
import contextlib
from dataclasses import replace
import hashlib
import errno
import fcntl
import importlib.util
import io
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

REAL_MONOTONIC = time.monotonic
TEST_DARWIN_PS = os.environ.get("ENVY_RUNTIME_TEST_DARWIN_PS")
MOCK_DARWIN_PS = "/nix/store/" + "a" * 32 + "-ps/bin/ps"

SPEC = importlib.util.spec_from_file_location("runtime_vm", Path(__file__).with_name("vm.py"))
vm = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = vm
SPEC.loader.exec_module(vm)

FAKE_TOOL = r'''
import fcntl, hashlib, json, os, pathlib, subprocess, sys, time
root = pathlib.Path(__file__).parent / 'observations'
root.mkdir(exist_ok=True)
work = pathlib.Path.cwd()
role = pathlib.Path(sys.argv[0]).name
settings = json.loads((pathlib.Path(__file__).parent / 'settings.json').read_text())
call = {'role': role, 'args': sys.argv[1:], 'env': dict(os.environ), 'cwd': str(work), 'reads': []}
base_path = None
if role == 'qemu-img':
    if sys.argv[1] == 'info': base_path = sys.argv[-1]
    if sys.argv[1] == 'create': base_path = sys.argv[sys.argv.index('-b') + 1]
    if sys.argv[1] == 'resize': base_path = (root / 'overlay-base-path').read_text()
if role == 'qemu':
    nodes = [json.loads(sys.argv[i + 1]) for i, arg in enumerate(sys.argv) if arg == '-blockdev']
    assert nodes[0]['backing']['file']['filename'] == '/dev/fdset/1'
    registration = sys.argv[sys.argv.index('-add-fd') + 1]
    fields = dict(field.split('=', 1) for field in registration.split(','))
    assert fields['set'] == '1' and fields['opaque'] == 'verified-base'
    base_path = '/dev/fd/' + fields['fd']
if base_path is not None:
    assert base_path.startswith('/dev/fd/')
    inherited = int(base_path.removeprefix('/dev/fd/'))
    assert fcntl.fcntl(inherited, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
    with open(base_path, 'rb') as base:
        info = os.fstat(base.fileno())
        call['base'] = {'device': info.st_dev, 'inode': info.st_ino,
                        'sha256': hashlib.sha256(os.pread(base.fileno(), info.st_size, 0)).hexdigest()}
def read_native(reference):
    with open(reference, 'rb') as stream:
        info = os.fstat(stream.fileno())
        # Native block readers seek; /dev/fd may share an offset on Darwin.
        data = os.pread(stream.fileno(), info.st_size, 0)
    call['reads'].append({'sha256': hashlib.sha256(data).hexdigest(),
                          'inode': info.st_ino, 'mode': info.st_mode & 0o777})
    record()
    return data

def record():
    with (root / 'fake-calls.jsonl').open('a') as stream:
        stream.write(json.dumps(call) + '\n')
record()
def start_child():
    ready = root / 'child-ready.json'
    ready.unlink(missing_ok=True)
    (root / 'child.pid').unlink(missing_ok=True)
    leader = {'pid': os.getpid(), 'ppid': os.getppid(), 'pgid': os.getpgrp()}
    stage = ('image-info' if sys.argv[1] == 'info' else 'overlay-' + sys.argv[1]) if role == 'qemu-img' else 'qemu'
    code = r"""
import fcntl, json, os, pathlib, stat, sys, time
lease, destination, leader, stage = sys.argv[1:]
fd = os.open(lease, os.O_RDWR | os.O_NOFOLLOW)
info = os.fstat(fd)
assert stat.S_ISREG(info.st_mode)
fcntl.flock(fd, fcntl.LOCK_EX)
leader = json.loads(leader)
assert os.getppid() == leader['pid']
assert os.getpgrp() == leader['pgid'] == leader['pid']
assert stage in ('image-info', 'overlay-create', 'overlay-resize', 'qemu')
row = {'leader': leader, 'stage': stage,
       'child': {'pid': os.getpid(), 'ppid': os.getppid(), 'pgid': os.getpgrp()},
       'lease': [info.st_dev, info.st_ino]}
ready = pathlib.Path(destination)
part = ready.with_suffix('.part')
part.write_text(json.dumps(row))
# Publish PID evidence while holding the lifetime FD, before the ready barrier.
(ready.parent / 'child.pid').write_text(str(os.getpid()))
part.replace(ready)
# Keep the exact locked FD open until OS process exit, including zombie state.
if json.loads((ready.parent.parent / 'settings.json').read_text()).get('mode') == 'child-exit':
    os._exit(0)
if stage == 'qemu' and json.loads((ready.parent.parent / 'settings.json').read_text()).get('mode') == 'closed-stdio':
    os.close(1)
    os.close(2)
time.sleep(60)
"""
    child = subprocess.Popen([sys.executable, '-c', code, str(root.parent / 'child.lease'),
                              str(ready), json.dumps(leader), stage])
    deadline = time.monotonic() + 5
    while not ready.exists():
        assert time.monotonic() < deadline, 'child handshake failed'
        time.sleep(0.005)
    return child

if role == 'qemu-img' and settings.get('prepare_cancel') == sys.argv[1]:
    start_child()
    time.sleep(60)
if role == 'qemu-img':
    if sys.argv[1] == 'info':
        print(json.dumps(settings.get('info', {'format': 'qcow2', 'virtual-size': 2147483648})))
    elif sys.argv[1] == 'create':
        pathlib.Path(sys.argv[-1]).write_bytes(b'synthetic overlay')
        (root / 'overlay-base-path').write_text(base_path)
    elif sys.argv[1] == 'resize':
        assert sys.argv[-1] == '40G'
        read_native(sys.argv[-2])
        with open(sys.argv[-2], 'ab') as stream: stream.write(b' resized')
        record()
    sys.exit(settings.get('image_exit', 0))
if role == 'mkisofs':
    payloads = {}
    if '-graft-points' in sys.argv:
        assert '-follow-links' in sys.argv
        assert sys.argv[sys.argv.index('-file-mode') + 1] == '0444'
        assert sys.argv[sys.argv.index('-dir-mode') + 1] == '0555'
    inputs = sys.argv[sys.argv.index('-graft-points') + 1:] if '-graft-points' in sys.argv else sys.argv[-3:]
    for item in inputs:
        leaf, reference = item.split('=', 1) if '=' in item else (item, item)
        if reference.startswith('/dev/fd/'):
            descriptor = int(reference.rsplit('/', 1)[1])
            assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
            assert os.fstat(descriptor).st_nlink == 0
        payloads[leaf] = read_native(reference).decode()
        (root / leaf).write_text(payloads[leaf])
    (root / 'seed-observed.json').write_text(json.dumps({
        leaf: hashlib.sha256(body.encode()).hexdigest() for leaf, body in payloads.items()}))
    pathlib.Path(sys.argv[sys.argv.index('-output') + 1]).write_text(json.dumps(payloads))
    record()
    sys.exit(0)
registrations = {}
for i, arg in enumerate(sys.argv):
    if arg == '-add-fd':
        fields = dict(field.split('=', 1) for field in sys.argv[i + 1].split(','))
        registrations[fields['set']] = '/dev/fd/' + fields['fd']
def block_reference(node):
    reference = node['file']['filename']
    return registrations[reference.rsplit('/', 1)[1]] if reference.startswith('/dev/fdset/') else reference
if registrations.keys() >= {'1', '2', '3'}:
    for group, access in (('1', os.O_RDONLY), ('2', os.O_RDWR), ('3', os.O_RDONLY)):
        descriptor = int(registrations[group].rsplit('/', 1)[1])
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == access
assert read_native(block_reference(nodes[0])) == b'synthetic overlay resized'
seed = json.loads(read_native(block_reference(nodes[1])))
record()
mode = settings.get('mode', 'good')
if mode in ('hang', 'flood', 'cancel', 'leader-exit', 'closed-stdio', 'child-exit'):
    child = start_child()
    if mode in ('leader-exit', 'closed-stdio', 'child-exit'):
        if mode == 'child-exit':
            assert child.wait(timeout=3) == 0
        if mode == 'closed-stdio':
            os.close(1)
            os.close(2)
        sys.exit(0)
    if mode == 'flood':
        while True:
            os.write(1, b'x' * 8192)
    time.sleep(60)
if mode == 'none':
    sys.exit(0)
if mode == 'exit-error':
    sys.stderr.write('synthetic private diagnostic')
    sys.exit(7)
token = json.loads(seed['meta-data'])['instance-id'].removeprefix('envy-runtime-')
receipt = {'token': token, 'arch': 'x86_64', 'nixPresent': False}
if mode == 'wrong-token': receipt['token'] = 'wrong'
if mode == 'wrong-arch': receipt['arch'] = 'aarch64'
if mode == 'nix': receipt['nixPresent'] = True
if mode == 'zero': receipt['nixPresent'] = 0
print('ENVY_RUNTIME_BOOT_RECEIPT ' + json.dumps(receipt), flush=True)
if mode == 'duplicate': print('ENVY_RUNTIME_BOOT_RECEIPT ' + json.dumps(receipt))
if mode == 'receipt-error': sys.exit(9)
'''


class RunnerLeaseTests(unittest.TestCase):
    def setUp(self, *, darwin_ps=TEST_DARWIN_PS):
        temporary = tempfile.TemporaryDirectory(prefix='envy-host-lease-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        self.addCleanup(os.close, self.fd)
        self.runner = vm.Runner(self.root, [str(self.root / 'fake')],
                                time.monotonic() + 10, 65536, directory_fd=self.fd,
                                darwin_ps=darwin_ps)
        self.process = MagicMock(pid=456789, returncode=None)
        self.events = []
        self.process.poll.side_effect = AssertionError('poll reaps the lease')
        def reap(**kwargs):
            self.events.append('wait')
            self.process.returncode = self.exit_code
            return self.exit_code
        self.log_fds = []
        original_file = vm.exclusive_file
        @contextlib.contextmanager
        def tracked_file(*args, **kwargs):
            with original_file(*args, **kwargs) as stream:
                self.log_fds.append(stream.fileno())
                yield stream
        file_patch = patch.object(vm, 'exclusive_file', tracked_file)
        file_patch.start()
        self.addCleanup(file_patch.stop)
        self.exit_code = 0
        self.process.wait.side_effect = reap
        self.selector = MagicMock()
        self.selector.get_map.return_value = {}
        self.selector.select.return_value = []
        self.observations = []
        def observe(kind, pid, flags):
            self.assertEqual(kind, os.P_PID)
            self.assertEqual(flags, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            if pid == os.getpid():
                raise ChildProcessError()
            self.assertEqual(pid, self.process.pid)
            self.events.append('observe')
            value = self.observations.pop(0) if self.observations else self.observed()
            if isinstance(value, BaseException):
                raise value
            return value
        self.waitid = patch.object(vm.os, 'waitid', side_effect=observe).start()
        self.addCleanup(patch.stopall)
        self.spawn = patch.object(vm.subprocess, 'Popen', return_value=self.process).start()
        patch.object(vm.selectors, 'DefaultSelector', return_value=self.selector).start()
        self.kill = patch.object(vm.os, 'killpg', side_effect=lambda *args: self.events.append('stop')).start()

    def observed(self, code=None, status=None):
        return SimpleNamespace(si_pid=self.process.pid,
                               si_code=os.CLD_EXITED if code is None else code,
                               si_status=self.exit_code if status is None else status)

    def run_fake(self):
        return self.runner.run('fixture', [str(self.root / 'fake')])

    def assert_closed(self):
        for fd in self.log_fds:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.selector.close.assert_called_once()
        self.process.stdout.close.assert_called_once()
        self.process.stderr.close.assert_called_once()
        for leaf in ('fixture.stdout.log', 'fixture.stderr.log'):
            self.assertTrue((self.root / leaf).is_file())

    def test_nonreaping_flags_stop_before_wait_and_no_signal_after_wait(self):
        self.observations = [None, self.observed()]
        self.assertEqual(self.run_fake(), b'')
        self.assertLess(self.events.index('stop'), self.events.index('wait'))
        self.assertEqual(self.events.count('stop'), 1)
        self.process.poll.assert_not_called()
        self.assertEqual(self.runner.commands[-1]['returncode'], 0)
        self.assert_closed()

    def test_exit_and_signal_codes_are_preserved(self):
        for code, status, expected in ((os.CLD_EXITED, 17, 17), (os.CLD_KILLED, 9, -9),
                                       (os.CLD_DUMPED, 6, -6)):
            with self.subTest(code=code):
                # Each run gets exclusively created log files.
                self.runner.root = self.root
                self.process.returncode = None
                self.exit_code = expected
                self.observations = [self.observed(code, status), self.observed(code, status)]
                with self.assertRaisesRegex(vm.ProbeError, 'tool-failed:'):
                    self.runner.run('code' + str(code), ['fake'])
                self.assertEqual(self.runner.commands[-1]['returncode'], expected)

    def test_echild_and_early_returncode_revoke_signal_authority(self):
        for index, early in enumerate((False, True)):
            with self.subTest(early=early):
                self.process.returncode = 0 if early else None
                self.observations = [ChildProcessError()]
                self.kill.reset_mock()
                with self.assertRaises((vm.ProbeError, ChildProcessError)):
                    self.runner.run('revoked' + str(index), ['fake'])
                self.kill.assert_not_called()
                self.assertIsNone(self.runner.commands[-1]['returncode'])

    def test_unsupported_api_fails_before_spawn(self):
        for value in (None, 0):
            with self.subTest(value=value), patch.object(vm.os, 'WNOWAIT', value):
                with self.assertRaises(vm.ProbeError):
                    self.run_fake()
                self.spawn.assert_not_called()
        with patch.object(vm.os, 'waitid', side_effect=NotImplementedError):
            with self.assertRaises(vm.ProbeError):
                self.run_fake()
            self.spawn.assert_not_called()

    def test_unknown_waitid_result_fails_closed(self):
        self.observations = [self.observed(999, 0)]
        with self.assertRaises(vm.ProbeError):
            self.run_fake()
        self.kill.assert_not_called()
        self.assertIsNone(self.runner.commands[-1]['returncode'])
        self.assert_closed()

    def test_open_stdio_descendant_is_stopped_as_soon_as_leader_exits(self):
        self.selector.get_map.return_value = {'held': True}
        def select(**kwargs):
            if 'stop' in self.events:
                self.selector.get_map.return_value = {}
            return []
        self.selector.select.side_effect = select
        self.run_fake()
        self.assertLess(self.events.index('stop'), self.events.index('wait'))
        self.assert_closed()

    def test_cleanup_attempts_every_close_and_preserves_first_error(self):
        first = OSError('first-stop-error')
        self.kill.side_effect = first
        self.process.wait.side_effect = OSError('later-wait-error')
        self.process.stdout.close.side_effect = OSError('later-close-error')
        self.selector.close.side_effect = OSError('later-selector-error')
        with self.assertRaises(OSError) as caught:
            self.run_fake()
        self.assertIs(caught.exception, first)
        self.process.wait.assert_called_once()
        self.assert_closed()

    def test_log_context_exit_errors_close_both_fds_and_keep_first_error(self):
        first = OSError('first-context-exit')
        original_file = vm.exclusive_file
        @contextlib.contextmanager
        def failing_exit(path, **kwargs):
            try:
                with original_file(path, **kwargs) as stream:
                    yield stream
            finally:
                if path.endswith('.stderr.log'):
                    raise first
                raise OSError('later-context-exit')
        with patch.object(vm, 'exclusive_file', failing_exit):
            with self.assertRaises(OSError) as caught:
                self.run_fake()
        self.assertIs(caught.exception, first)
        self.assert_closed()

    def test_stop_requires_explicit_owned_lease(self):
        with self.assertRaisesRegex(vm.ProbeError, 'process-lease-required'):
            vm.Runner.stop(self.process)
        self.kill.assert_not_called()

    def test_cleanup_error_is_not_hidden_by_callers_handled_exception(self):
        first = OSError('first-stop-error')
        self.kill.side_effect = first
        try:
            raise ValueError('callers-handled-error')
        except ValueError:
            with self.assertRaises(OSError) as caught:
                self.run_fake()
        self.assertIs(caught.exception, first)
        self.assert_closed()

    def test_primary_cancel_survives_cleanup_errors(self):
        self.observations = [None]
        self.selector.select.side_effect = KeyboardInterrupt
        self.selector.get_map.return_value = {'open': True}
        self.kill.side_effect = OSError('cleanup-stop')
        with self.assertRaises(KeyboardInterrupt):
            self.run_fake()
        self.assert_closed()


class DarwinZombieLeaseTests(unittest.TestCase):
    def setUp(self):
        self.native_selector = vm.selectors.DefaultSelector
        # These paths exist only inside mocked Popen/OS boundaries, on either host.
        def trusted_tool(value):
            self.assertEqual(value, MOCK_DARWIN_PS)
            return MOCK_DARWIN_PS
        validator = patch.object(vm, 'trusted_darwin_ps', side_effect=trusted_tool)
        validator.start()
        self.addCleanup(validator.stop)
        native_tmp = patch.object(vm, 'native_tmp', return_value='/synthetic-native-tmp')
        native_tmp.start()
        self.addCleanup(native_tmp.stop)
        self.fixture = RunnerLeaseTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp(darwin_ps=MOCK_DARWIN_PS)
        self.fixture.exit_code = 17
        self.permission = PermissionError(errno.EPERM, 'synthetic-denied')
        self.fixture.kill.side_effect = self.permission
        self.flags = os.WEXITED | os.WNOHANG | os.WNOWAIT
        self.platform = patch.object(vm.sys, 'platform', 'darwin')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.set_query(b' 456789 456789 Z\n')

    def set_query(self, stdout, stderr=b'', code=0):
        self.query = MagicMock(pid=567890, returncode=None)
        for name, data in (('stdout', stdout), ('stderr', stderr)):
            reader, writer = os.pipe()
            stream = os.fdopen(reader, 'rb', buffering=0)
            def write_all(writer=writer, data=data):
                try:
                    offset = 0
                    while offset < len(data):
                        offset += os.write(writer, data[offset:])
                except BrokenPipeError:
                    pass
                finally:
                    os.close(writer)
            worker = threading.Thread(target=write_all, daemon=True)
            worker.start()
            def close_and_join(stream=stream, worker=worker):
                stream.close()
                worker.join(timeout=1)
                self.assertFalse(worker.is_alive(), 'fixture writer did not close')
            self.addCleanup(close_and_join)
            setattr(self.query, name, stream)
        def wait(**kwargs):
            self.query.returncode = code
            return code
        self.query.wait.side_effect = wait
        self.fixture.spawn.side_effect = lambda argv, **kwargs: self.query if argv[0] == MOCK_DARWIN_PS else self.fixture.process
        self.fixture.spawn.reset_mock()
        self.query_selector = self.native_selector()
        self.addCleanup(self.query_selector.close)
        vm.selectors.DefaultSelector.side_effect = lambda: self.query_selector

    def lease(self):
        return vm._ProcessLease(self.fixture.process, self.flags, darwin_ps=MOCK_DARWIN_PS)

    def assert_denied(self):
        lease = self.lease()
        with self.assertRaises(PermissionError) as caught:
            lease.stop()
        self.assertIs(caught.exception, self.permission)
        self.assertEqual(self.fixture.kill.call_count, 1)
        self.assertIsNone(getattr(lease, 'cleanup_evidence', None))
        return lease

    def test_exact_query_zombie_evidence_and_unchanged_nonreaped_exit(self):
        lease = self.lease()
        lease.stop()
        self.assertEqual(lease.cleanup_evidence, {
            'kind': 'darwin-owned-group-metadata', 'querySuccess': True,
            'allObservedMembersZombie': True, 'count': 1})
        call = self.fixture.spawn.call_args
        self.assertEqual(call.args[0], [MOCK_DARWIN_PS, '-g', '456789', '-o', 'pid=,pgid=,state='])
        self.assertEqual(call.kwargs['cwd'], str(Path(MOCK_DARWIN_PS).parent))
        self.assertEqual(call.kwargs['env'], {'HOME': str(Path(MOCK_DARWIN_PS).parent), 'TMPDIR': vm.native_tmp(),
                                            'PATH': str(Path(MOCK_DARWIN_PS).parent), 'LC_ALL': 'C', 'TZ': 'UTC'})
        self.assertIs(call.kwargs['shell'], False)
        self.assertIs(call.kwargs['close_fds'], True)
        self.assertEqual(call.kwargs['pass_fds'], ())
        self.assertEqual(self.fixture.waitid.call_count, 3)
        self.fixture.process.poll.assert_not_called()
        self.fixture.process.wait.assert_not_called()
        self.query.kill.assert_not_called()
        self.assertTrue(self.query.stdout.closed)
        self.assertTrue(self.query.stderr.closed)
        lease.reap()
        lease.stop()
        self.assertEqual(self.fixture.kill.call_count, 1)
        self.assertEqual(self.fixture.process.returncode, 17)

    def test_all_observed_members_must_be_zombie_in_exact_group(self):
        self.set_query(b'456789 456789 Z\n456790 456789 Z\n')
        lease = self.lease()
        lease.stop()
        self.assertEqual(lease.cleanup_evidence['count'], 2)

    def test_live_wrong_group_duplicates_empty_and_malformed_are_denied(self):
        cases = (b'456789 456789 Z\n456790 456789 S\n', b'456789 1 Z\n',
                 b'456789 456789 Z\n456789 456789 Z\n', b'', b'456790 456789 Z\n',
                 b'456789 456789 Z+\n', b'456789 456789 Z extra\n', b'456789 456789 Z\n\n',
                 b'0 456789 Z\n', b'2147483648 456789 Z\n', b'456789 456789 \xff\n')
        for data in cases:
            with self.subTest(data=data):
                self.fixture.kill.reset_mock()
                self.set_query(data)
                self.assert_denied()
                self.assertTrue(self.query.stdout.closed)
                self.assertTrue(self.query.stderr.closed)

    def test_query_stderr_and_nonzero_exit_are_denied(self):
        for stderr, code in ((b'untrusted', 0), (b'', 1)):
            with self.subTest(stderr=stderr, code=code):
                self.fixture.kill.reset_mock()
                self.set_query(b'456789 456789 Z\n', stderr, code)
                self.assert_denied()

    def test_byte_and_line_caps_are_denied(self):
        for data in (b'x' * 8193, b'456789 456789 Z\n' * 129):
            with self.subTest(size=len(data)):
                self.fixture.kill.reset_mock()
                self.set_query(data)
                self.assert_denied()
                self.query.kill.assert_called_once()
                self.query.wait.assert_called_once()

    def test_query_timeout_kills_only_own_ps_and_waits(self):
        self.query.wait.side_effect = [subprocess.TimeoutExpired(MOCK_DARWIN_PS, 2), 0]
        self.assert_denied()
        self.query.kill.assert_called_once()
        self.assertEqual(self.query.wait.call_count, 2)
        self.assertTrue(self.query.stdout.closed)
        self.assertTrue(self.query.stderr.closed)

    def test_collection_deadline_is_bounded(self):
        selector = MagicMock()
        selector.get_map.return_value = {'open': True}
        selector.select.return_value = []
        vm.selectors.DefaultSelector.side_effect = lambda: selector
        with patch.object(vm, 'time', SimpleNamespace(monotonic=MagicMock(side_effect=[0, 0, 3]))):
            self.assert_denied()
        self.query.kill.assert_called_once()
        self.query.wait.assert_called_once()
        selector.close.assert_called_once()

    def test_identity_revoked_or_waitid_changed_after_query_is_denied(self):
        first = self.fixture.observed()
        cases = ([first, first, ChildProcessError()],
                 [first, first, self.fixture.observed(status=18)], [first, first, None])
        for observations in cases:
            with self.subTest(observations=observations):
                self.fixture.kill.reset_mock()
                self.set_query(b'456789 456789 Z\n')
                self.fixture.observations = list(observations)
                lease = self.assert_denied()
                self.assertFalse(lease.can_stop)
        self.fixture.kill.reset_mock()
        self.fixture.observations = []
        self.set_query(b'456789 456789 Z\n')
        def early_reap(**kwargs):
            self.fixture.process.returncode = 17
            return 0
        self.query.wait.side_effect = early_reap
        self.assertFalse(self.assert_denied().can_stop)

    def test_no_exit_non_darwin_and_missing_lease_never_query(self):
        self.fixture.observations = [None]
        self.assert_denied()
        self.fixture.spawn.assert_not_called()
        self.fixture.kill.reset_mock()
        with patch.object(vm.sys, 'platform', 'linux'):
            self.assert_denied()
        self.fixture.spawn.assert_not_called()
        self.fixture.process.returncode = 17
        self.fixture.kill.reset_mock()
        with self.assertRaises(vm.ProbeError):
            self.lease().stop()
        self.fixture.kill.assert_not_called()
        self.fixture.spawn.assert_not_called()

    def test_full_128_row_boundary_and_exact_byte_limit_are_accepted(self):
        for data, count in ((b'456789 456789 Z\n' + b''.join(
                str(pid).encode() + b' 456789 Z\n' for pid in range(456790, 456917)), 128),
                (b' ' * (8192 - len(b'456789 456789 Z\n')) + b'456789 456789 Z\n', 1)):
            with self.subTest(count=count):
                self.fixture.kill.reset_mock()
                self.set_query(data)
                lease = self.lease()
                lease.stop()
                self.assertEqual(lease.cleanup_evidence['count'], count)
                self.query.kill.assert_not_called()

    def test_changed_prequery_exit_and_outer_deadline_do_not_spawn(self):
        first = self.fixture.observed()
        self.fixture.observations = [first, self.fixture.observed(status=18)]
        self.assertFalse(self.assert_denied().can_stop)
        self.fixture.spawn.assert_not_called()
        self.fixture.kill.reset_mock()
        self.fixture.observations = []
        lease = vm._ProcessLease(self.fixture.process, self.flags, deadline=0, darwin_ps=MOCK_DARWIN_PS)
        with self.assertRaises(PermissionError) as caught:
            lease.stop()
        self.assertIs(caught.exception, self.permission)
        self.fixture.spawn.assert_not_called()

    def test_query_kill_error_still_waits_and_closes_every_pipe(self):
        self.query.wait.side_effect = [subprocess.TimeoutExpired(MOCK_DARWIN_PS, 1), 0]
        self.query.kill.side_effect = OSError('query-kill-error')
        self.assert_denied()
        self.assertEqual(self.query.wait.call_count, 2)
        self.assertTrue(self.query.stdout.closed)
        self.assertTrue(self.query.stderr.closed)

    def test_selector_close_error_cannot_publish_success_evidence(self):
        selector = self.query_selector
        original_close = selector.close
        def close_error():
            original_close()
            raise OSError('query-selector-close')
        with patch.object(selector, 'close', side_effect=close_error):
            self.assert_denied()
        self.assertTrue(self.query.stdout.closed)
        self.assertTrue(self.query.stderr.closed)

    def test_query_cancel_keeps_primary_and_attempts_all_cleanup(self):
        self.query.kill.side_effect = OSError('query-kill-error')
        with patch.object(vm.os, 'read', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.lease().stop()
        self.query.kill.assert_called_once()
        self.query.wait.assert_called_once()
        self.assertTrue(self.query.stdout.closed)
        self.assertTrue(self.query.stderr.closed)

    def test_identity_revoked_during_query_close_cannot_publish_evidence(self):
        selector = self.query_selector
        original_close = selector.close
        def close_and_revoke():
            original_close()
            self.fixture.process.returncode = 17
        with patch.object(selector, 'close', side_effect=close_and_revoke):
            self.assertFalse(self.assert_denied().can_stop)
        self.assertTrue(self.query.stdout.closed)
        self.assertTrue(self.query.stderr.closed)

    def test_private_query_also_rejects_non_darwin_before_spawn(self):
        lease = self.lease()
        lease.observe()
        with patch.object(vm.sys, 'platform', 'linux'):
            with self.assertRaises(vm.ProbeError):
                lease._darwin_zombies()
        self.fixture.spawn.assert_not_called()
        self.fixture.kill.assert_not_called()

    def test_query_spawn_and_close_errors_preserve_original_permission_error(self):
        self.fixture.spawn.side_effect = OSError('query-spawn')
        self.assert_denied()
        self.fixture.kill.reset_mock()
        self.set_query(b'456789 456789 Z\n')
        original = self.query.stdout
        close = MagicMock()
        def close_error():
            original.close()
            raise OSError('query-close')
        close.side_effect = close_error
        proxy = MagicMock()
        proxy.fileno.side_effect = original.fileno
        proxy.close = close
        self.query.stdout = proxy
        self.assert_denied()
        self.assertTrue(original.closed)
        self.assertTrue(self.query.stderr.closed)


class DarwinPsPathTests(unittest.TestCase):
    def setUp(self):
        self.platform = patch.object(vm.sys, 'platform', 'darwin')
        self.platform.start()
        self.addCleanup(self.platform.stop)
        self.tool = MOCK_DARWIN_PS
        expected = patch.dict(vm.os.environ, {'ENVY_RUNTIME_DARWIN_PS': self.tool}, clear=True)
        expected.start()
        self.addCleanup(expected.stop)
        self.target = '/nix/store/' + 'b' * 32 + '-adv-cmds/bin/ps'
        self.nodes = {}
        self.links = {}
        for path in (Path('/nix'), Path('/nix/store'), *Path(self.tool).parents):
            self.nodes[str(path)] = SimpleNamespace(st_mode=stat.S_IFDIR | 0o555, st_uid=0)
        self.nodes[self.tool] = SimpleNamespace(st_mode=stat.S_IFREG | 0o555, st_uid=0)
        def lstat(path):
            if str(path) not in self.nodes:
                raise FileNotFoundError()
            return self.nodes[str(path)]
        patch.object(vm.Path, 'lstat', lstat).start()
        patch.object(vm.os, 'readlink', side_effect=lambda p: self.links[str(p)]).start()
        self.addCleanup(patch.stopall)

    def reject(self, value):
        with patch.object(vm.subprocess, 'Popen') as spawn:
            with self.assertRaises(vm.ProbeError):
                vm.trusted_darwin_ps(value)
            spawn.assert_not_called()

    def test_immutable_root_owned_regular_is_accepted(self):
        self.assertEqual(vm.trusted_darwin_ps(self.tool), self.tool)

    def test_missing_expected_identity_rejects_candidate_and_raw_config_before_spawn(self):
        with patch.dict(vm.os.environ, {}, clear=True):
            self.reject(self.tool)
            with patch.object(vm.subprocess, 'Popen') as spawn:
                with self.assertRaises(vm.ProbeError):
                    vm.Runner(Path('/unused'), ['/unused/qemu'], 10, 65536,
                              directory_fd=-1, darwin_ps=self.tool)
                config = vm.BootConfig(workdir='/unused', base='/unused/base', sha256='0' * 64,
                                       qemu='/unused/qemu', qemu_img='/unused/img', mkisofs='/unused/iso',
                                       darwin_ps=self.tool)
                self.assertEqual(vm.boot_probe(config)['error'], 'darwin-ps-identity-unavailable')
                spawn.assert_not_called()

    def test_other_root_owned_readonly_store_helper_is_rejected_before_spawn(self):
        helper = '/nix/store/' + 'c' * 32 + '-helper/bin/ps'
        for path in Path(helper).parents:
            self.nodes[str(path)] = SimpleNamespace(st_mode=stat.S_IFDIR | 0o555, st_uid=0)
        self.nodes[helper] = SimpleNamespace(st_mode=stat.S_IFREG | 0o555, st_uid=0)
        self.assertEqual(vm.trusted_darwin_ps(self.tool), self.tool)
        self.reject(helper)
        with patch.object(vm.subprocess, 'Popen') as spawn:
            with self.assertRaises(vm.ProbeError):
                vm.Runner(Path('/unused'), ['/unused/qemu'], 10, 65536,
                          directory_fd=-1, darwin_ps=helper)
            spawn.assert_not_called()
        process = MagicMock(pid=456789, returncode=None)
        observed = SimpleNamespace(si_pid=process.pid, si_code=os.CLD_EXITED, si_status=17)
        permission = PermissionError(errno.EPERM, 'synthetic-denied')
        with patch.object(vm.os, 'waitid', return_value=observed), \
                patch.object(vm.os, 'killpg', side_effect=permission), \
                patch.object(vm.subprocess, 'Popen') as spawn:
            lease = vm._ProcessLease(process, os.WEXITED | os.WNOHANG | os.WNOWAIT, darwin_ps=helper)
            with self.assertRaises(PermissionError) as caught:
                lease.stop()
            self.assertIs(caught.exception, permission)
            self.assertIsNone(lease.cleanup_evidence)
            spawn.assert_not_called()

    def test_alias_to_same_expected_target_is_accepted(self):
        alias = str(Path(self.tool).with_name('ps-alias'))
        self.nodes[alias] = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
        self.links[alias] = 'ps'
        self.assertEqual(vm.trusted_darwin_ps(alias), self.tool)
        with patch.dict(vm.os.environ, {'ENVY_RUNTIME_DARWIN_PS': alias}, clear=True):
            self.assertEqual(vm.trusted_darwin_ps(self.tool), self.tool)

    def test_expected_identity_itself_must_pass_file_checks(self):
        with patch.dict(vm.os.environ, {'ENVY_RUNTIME_DARWIN_PS': '/tmp/helper'}, clear=True):
            self.reject(self.tool)

    def test_missing_relative_mutable_and_argv_are_rejected(self):
        for value in (None, '', 'ps', '/bin/ps', '/tmp/ps', [self.tool], self.tool + '/..'):
            with self.subTest(kind=type(value).__name__):
                self.reject(value)
        del self.nodes[self.tool]
        self.reject(self.tool)

    def test_nonregular_nonexecutable_writable_suid_sgid_and_foreign_owner_rejected(self):
        for mode in (stat.S_IFDIR | 0o555, stat.S_IFIFO | 0o444, stat.S_IFREG | 0o444,
                     stat.S_IFREG | 0o755, stat.S_IFREG | 0o4555, stat.S_IFREG | 0o2555):
            with self.subTest(mode=mode):
                self.nodes[self.tool] = SimpleNamespace(st_mode=mode, st_uid=0)
                self.reject(self.tool)
        self.nodes[self.tool] = SimpleNamespace(st_mode=stat.S_IFREG | 0o555, st_uid=501)
        self.reject(self.tool)

    def test_immutable_store_symlink_resolves_to_checked_target(self):
        for path in Path(self.target).parents:
            self.nodes[str(path)] = SimpleNamespace(st_mode=stat.S_IFDIR | 0o555, st_uid=0)
        self.nodes[self.tool] = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
        self.nodes[self.target] = SimpleNamespace(st_mode=stat.S_IFREG | 0o555, st_uid=0)
        for target in (self.target, '../../' + Path(self.target).relative_to('/nix/store').as_posix()):
            with self.subTest(relative=not target.startswith('/')):
                self.links[self.tool] = target
                self.assertEqual(vm.trusted_darwin_ps(self.tool), self.target)
        self.nodes[self.target].st_mode |= stat.S_ISUID
        self.reject(self.tool)
        self.nodes[self.target].st_mode = stat.S_IFREG | 0o2555
        self.reject(self.tool)

    def test_symlink_escape_loop_missing_target_and_writable_ancestor_rejected(self):
        self.nodes[self.tool] = SimpleNamespace(st_mode=stat.S_IFLNK | 0o777, st_uid=0)
        for target in ('/tmp/ps', '../../../../tmp/ps', self.tool, self.target):
            with self.subTest(target=target):
                self.links[self.tool] = target
                self.reject(self.tool)
        self.nodes[self.tool] = SimpleNamespace(st_mode=stat.S_IFREG | 0o555, st_uid=0)
        self.nodes[str(Path(self.tool).parent)].st_mode |= stat.S_IWUSR
        self.reject(self.tool)

    def test_linux_does_not_require_or_inspect_darwin_tool(self):
        with patch.object(vm.sys, 'platform', 'linux'), patch.object(vm.Path, 'lstat') as query:
            self.assertIsNone(vm.trusted_darwin_ps(None))
            self.assertIsNone(vm.trusted_darwin_ps('/bin/ps'))
            query.assert_not_called()

    def test_unconfigured_runner_fails_before_native_spawn(self):
        with patch.object(vm.subprocess, 'Popen') as spawn:
            with self.assertRaisesRegex(vm.ProbeError, 'darwin-ps-unavailable'):
                vm.Runner(Path('/unused'), ['/unused/qemu'], 10, 65536, directory_fd=-1)
            spawn.assert_not_called()

    def test_unconfigured_lease_preserves_original_eperm_without_query(self):
        process = MagicMock(pid=456789, returncode=None)
        observed = SimpleNamespace(si_pid=process.pid, si_code=os.CLD_EXITED, si_status=17)
        permission = PermissionError(errno.EPERM, 'synthetic-denied')
        with patch.object(vm.os, 'waitid', return_value=observed), \
                patch.object(vm.os, 'killpg', side_effect=permission), \
                patch.object(vm.subprocess, 'Popen') as spawn:
            lease = vm._ProcessLease(process, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            with self.assertRaises(PermissionError) as caught:
                lease.stop()
            self.assertIs(caught.exception, permission)
            self.assertIsNone(lease.cleanup_evidence)
            spawn.assert_not_called()
            process.wait.assert_not_called()


class DarwinPsPackagingTests(unittest.TestCase):
    def test_three_wrappers_declare_and_pass_platform_scoped_public_tool(self):
        for name in ('boot.nix', 'install.nix', 'full.nix'):
            with self.subTest(wrapper=name):
                source = Path(__file__).with_name(name).read_text()
                self.assertIn('export ENVY_RUNTIME_DARWIN_PS=${pkgs.ps}/bin/ps', source)
                self.assertIn('pkgs.lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.ps ]', source)
                self.assertIn('pkgs.lib.optionalString pkgs.stdenv.hostPlatform.isDarwin "--darwin-ps ${pkgs.ps}/bin/ps"', source)

    def test_source_check_declares_same_public_package_for_native_tests(self):
        source = (Path(__file__).parents[2] / 'flake.nix').read_text()
        source = source.split('mkEnvySourceCheck = system:', 1)[1].split('mkGithubActionsCheck = system:', 1)[0]
        self.assertIn('lib.optionals pkgs.stdenv.hostPlatform.isDarwin [ pkgs.ps ]', source)
        self.assertIn("lib.optionalString pkgs.stdenv.hostPlatform.isDarwin ''", source)
        self.assertIn('export ENVY_RUNTIME_TEST_DARWIN_PS=${pkgs.ps}/bin/ps', source)
        self.assertIn('export ENVY_RUNTIME_DARWIN_PS=${pkgs.ps}/bin/ps', source)


class DarwinPsCliIdentityTests(unittest.TestCase):
    def test_repeated_candidate_is_rejected_at_all_three_entrypoints(self):
        import install
        import full
        entries = [(vm, 'boot_probe', 'boot-probe'), (install, 'install_probe', 'install-probe'),
                   (full, 'full_probe', 'full-probe')]
        for module, probe, command in entries:
            args = [command, '--workdir', '/unused', '--darwin-ps', MOCK_DARWIN_PS]
            if command == 'full-probe':
                for name in ('snapshot', 'source-store-path', 'base', 'sha256', 'qemu',
                             'qemu-img', 'mkisofs', 'nix-tar'):
                    args.extend(['--' + name, '/unused'])
                args.extend(['--snapshot-device', '1', '--snapshot-inode', '2'])
            with patch.object(module, probe, return_value={'ok': False}) as run, \
                    patch.object(vm.subprocess, 'Popen') as spawn:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(module.main(args), 1)
                self.assertEqual(run.call_args.args[0].darwin_ps, MOCK_DARWIN_PS)
                spawn.assert_not_called()
            for duplicate in (MOCK_DARWIN_PS, '/nix/store/' + 'c' * 32 + '-helper/bin/ps'):
                with self.subTest(command=command, duplicate=duplicate), \
                        patch.object(module, probe, side_effect=AssertionError('duplicate reached probe')) as run, \
                        patch.object(vm.subprocess, 'Popen') as spawn:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(module.main(args + ['--darwin-ps', duplicate]), 1)
                    self.assertEqual(json.loads(output.getvalue())['error'], 'invalid-arguments')
                    run.assert_not_called()
                    spawn.assert_not_called()


class BootProbeTests(unittest.TestCase):
    def setUp(self):
        self.darwin_ps = vm.trusted_darwin_ps(TEST_DARWIN_PS)
        self.temporary = tempfile.TemporaryDirectory(prefix="envy-vm-unit-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        (self.root / "base-dir").mkdir()
        self.base = self.root / "base-dir/base.qcow2"
        self.base_bytes = b"synthetic base: never a bootable VM image\n"
        self.base.write_bytes(self.base_bytes)
        self.base.chmod(0o444)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        self.settings = self.tools / "settings.json"
        self.lease_fd = os.open(self.tools / 'child.lease',
                                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.addCleanup(os.close, self.lease_fd)
        self.assertTrue(stat.S_ISREG(os.fstat(self.lease_fd).st_mode))
        self.owned_leaders = []
        original_spawn = subprocess.Popen
        def tracked_spawn(argv, *args, **kwargs):
            process = original_spawn(argv, *args, **kwargs)
            if argv[0] in [str(self.tools / name) for name in ('qemu', 'qemu-img', 'mkisofs')]:
                self.owned_leaders.append(process)
            return process
        tracker = patch.object(subprocess, 'Popen', side_effect=tracked_spawn)
        tracker.start()
        self.addCleanup(tracker.stop)
        original_selector = vm.selectors.DefaultSelector
        def controlled_selector():
            selector = original_selector()
            original_select = selector.select
            def select(*args, **kwargs):
                settings = json.loads(self.settings.read_text())
                ready = self.tools / 'observations/child-ready.json'
                if ready.exists() and (settings.get('mode') == 'cancel' or settings.get('prepare_cancel')):
                    # Cancellation is injected locally after the real child handshake.
                    raise KeyboardInterrupt
                return original_select(*args, **kwargs)
            selector.select = select
            return selector
        selector_patch = patch.object(vm.selectors, 'DefaultSelector', side_effect=controlled_selector)
        selector_patch.start()
        self.addCleanup(selector_patch.stop)
        self.configure()
        for name in ("qemu", "qemu-img", "mkisofs"):
            path = self.tools / name
            path.write_text("#!" + sys.executable + "\n" + FAKE_TOOL)
            path.chmod(0o700)
        self.config = vm.BootConfig(
            workdir=str(self.root / "owned"), base=str(self.base),
            sha256=hashlib.sha256(self.base_bytes).hexdigest(), qemu=str(self.tools / "qemu"),
            qemu_img=str(self.tools / "qemu-img"), mkisofs=str(self.tools / "mkisofs"),
            timeout=5, output_limit=65536, darwin_ps=TEST_DARWIN_PS,
        )

    def assert_query_spawn(self, argv, kwargs):
        self.assertEqual(argv[:2], [self.darwin_ps, '-g'])
        self.assertEqual(argv[3:], ['-o', 'pid=,pgid=,state='])
        self.assertTrue(argv[2].isdigit())
        held = [p for p in self.owned_leaders if p.pid == int(argv[2])]
        self.assertEqual(len(held), 1)
        self.assertIsNone(held[0].returncode)
        self.assertEqual(kwargs['pass_fds'], ())
        self.assertIs(kwargs['close_fds'], True)
        self.assertIs(kwargs['shell'], False)
        self.assertEqual(kwargs['cwd'], str(Path(self.darwin_ps).parent))
        self.assertEqual(kwargs['env'], {'HOME': str(Path(self.darwin_ps).parent), 'TMPDIR': vm.native_tmp(),
                                         'PATH': str(Path(self.darwin_ps).parent), 'LC_ALL': 'C', 'TZ': 'UTC'})

    def configure(self, **settings):
        ready = self.tools / 'observations/child-ready.json'
        ready.unlink(missing_ok=True)
        self.settings.write_text(json.dumps(settings))

    def assert_base_unchanged(self):
        self.assertEqual(self.base.read_bytes(), self.base_bytes)
        self.assertEqual(self.base.stat().st_mode & 0o777, 0o444)

    def calls(self):
        rows = [json.loads(line) for line in (self.tools / "observations/fake-calls.jsonl").read_text().splitlines()]
        calls = {}
        for row in rows:
            calls[(row['role'], tuple(row['args']))] = row
        return list(calls.values())

    probe = staticmethod(vm.boot_probe)

    def assert_canary(self, path, body, mode=0o640):
        self.assertEqual(path.read_bytes(), body)
        self.assertEqual(path.stat().st_mode & 0o777, mode)
        journal = self.tools / "observations/fake-calls.jsonl"
        if journal.exists():
            for call in self.calls():
                self.assertNotIn(hashlib.sha256(body).hexdigest(), [r['sha256'] for r in call['reads']])

    def test_mkdir_first_open_foreign_directory_is_untrusted(self):
        original = Path.mkdir
        work = self.root / 'owned'
        canary = b'foreign overlay at first open'
        def swap(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            if path == work:
                path.rename(self.root / 'discarded')
                original(path)
                path.chmod(0o750)
                (path / 'overlay.qcow2').write_bytes(canary)
                (path / 'overlay.qcow2').chmod(0o640)
            return result
        with patch.object(Path, 'mkdir', swap):
            result = self.probe(self.config)
        self.assert_canary(work / 'overlay.qcow2', canary)
        self.assertEqual(work.stat().st_mode & 0o777, 0o750)
        self.assertFalse((work / 'home').exists())
        self.assertFalse((work / 'tmp').exists())
        self.assertFalse(result['ok'])

    def test_native_stages_keep_overlay_and_seed_fds_after_leaf_replacement(self):
        runner_class = getattr(self, 'runner_class', vm.Runner)
        original = runner_class.run
        work, retained = self.root / 'owned', self.root / 'retained'
        overlay, iso = self.root / 'foreign-overlay', self.root / 'foreign-iso'
        overlay.write_bytes(b'foreign overlay between native stages')
        iso.write_bytes(b'foreign iso between native stages')
        overlay.chmod(0o640)
        iso.chmod(0o640)
        def swap(runner, stage, argv, **kwargs):
            if stage == 'overlay-resize':
                work.rename(retained)
                work.mkdir()
                for directory in (work, retained):
                    leaf = directory / 'overlay.qcow2'
                    if leaf.exists():
                        leaf.rename(directory / 'original-overlay')
                    leaf.symlink_to(overlay)
            if stage == 'qemu':
                for directory in (work, retained):
                    leaf = directory / 'seed.iso'
                    if leaf.exists():
                        leaf.rename(directory / 'original-seed')
                    leaf.symlink_to(iso)
            return original(runner, stage, argv, **kwargs)
        with patch.object(runner_class, 'run', swap):
            result = self.probe(self.config)
        self.assert_canary(overlay, b'foreign overlay between native stages')
        self.assert_canary(iso, b'foreign iso between native stages')
        self.assertTrue(result['ok'], result)

    def test_seed_leaf_replacement_before_qemu_never_reads_foreign_body(self):
        runner_class = getattr(self, 'runner_class', vm.Runner)
        original = runner_class.run
        work = self.root / 'owned'
        foreign = self.root / 'foreign-seed'
        foreign.write_bytes(b'foreign seed body is not an ISO')
        foreign.chmod(0o640)
        def swap(runner, stage, argv, **kwargs):
            if stage == 'qemu':
                (work / 'seed.iso').rename(work / 'retained-seed')
                (work / 'seed.iso').symlink_to(foreign)
            return original(runner, stage, argv, **kwargs)
        with patch.object(runner_class, 'run', swap):
            result = self.probe(self.config)
        self.assert_canary(foreign, b'foreign seed body is not an ISO')
        self.assertTrue(result['ok'], result)

    def test_iso_readonly_binding_rejects_foreign_regular_before_body_read(self):
        original = os.open
        foreign = self.root / 'foreign-iso-binding'
        foreign.write_bytes(b'foreign ISO binding')
        foreign.chmod(0o640)
        swapped = []
        def swap(path, flags, *args, **kwargs):
            if path == 'seed.iso' and not flags & os.O_CREAT:
                directory = kwargs['dir_fd']
                os.rename(path, 'retained-iso', src_dir_fd=directory, dst_dir_fd=directory)
                os.link(foreign, path, dst_dir_fd=directory)
                swapped.append(True)
            return original(path, flags, *args, **kwargs)
        with patch.object(os, 'open', swap):
            result = self.probe(self.config)
        self.assertTrue(swapped)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'seed-binding-changed')
        self.assertEqual(result['commands'], [])
        self.assert_canary(foreign, b'foreign ISO binding')

    def test_private_file_uses_held_fd_for_permissions_and_content(self):
        target, foreign = self.root / 'private-output', self.root / 'foreign-private'
        foreign.write_bytes(b'foreign private body')
        foreign.chmod(0o640)
        original = os.fdopen
        def swap(*args, **kwargs):
            stream = original(*args, **kwargs)
            target.rename(self.root / 'retained-private')
            target.symlink_to(foreign)
            return stream
        with patch.object(os, 'fdopen', swap):
            vm.private_file(target, 'owned body')
        self.assert_canary(foreign, b'foreign private body')
        self.assertEqual((self.root / 'retained-private').read_text(), 'owned body')
        with self.assertRaises(FileExistsError):
            vm.private_file(target, 'must not overwrite')
        self.assert_canary(foreign, b'foreign private body')

    def test_regular_seed_graft_rebinding_fails_without_exporting_foreign_body(self):
        original = os.open
        foreign = self.root / 'foreign-payload'
        foreign.write_bytes(b'foreign graft body')
        foreign.chmod(0o640)
        swapped = []
        def swap(path, flags, *args, **kwargs):
            if isinstance(path, str) and path.startswith('.seed-') and not flags & os.O_CREAT:
                directory = kwargs['dir_fd']
                os.rename(path, 'retained-payload', src_dir_fd=directory, dst_dir_fd=directory)
                os.link(foreign, path, dst_dir_fd=directory)
                swapped.append(True)
            return original(path, flags, *args, **kwargs)
        with patch.object(os, 'open', swap):
            result = self.probe(self.config)
        self.assertTrue(swapped)
        self.assertFalse(result['ok'])
        self.assertEqual(result['error'], 'seed-binding-changed')
        self.assertEqual(len(result['commands']), 3)
        self.assert_canary(foreign, b'foreign graft body')

    def test_log_creation_then_path_chmod_cannot_change_foreign_mode(self):
        original_open, original_fdopen = open, os.fdopen
        work = self.root / 'owned'
        foreign = self.root / 'foreign-log'
        foreign.write_bytes(b'foreign log body')
        foreign.chmod(0o640)
        swapped = []
        def replace_log(stream):
            if not swapped and (work / 'image-info.stdout.log').exists():
                (work / 'image-info.stdout.log').rename(work / 'original-log')
                (work / 'image-info.stdout.log').symlink_to(foreign)
                swapped.append(True)
            return stream
        def intercept_open(*args, **kwargs):
            return replace_log(original_open(*args, **kwargs))
        def intercept_fdopen(*args, **kwargs):
            return replace_log(original_fdopen(*args, **kwargs))
        with patch('builtins.open', intercept_open), patch.object(os, 'fdopen', intercept_fdopen):
            result = self.probe(self.config)
        self.assertTrue(swapped)
        self.assert_canary(foreign, b'foreign log body')
        self.assertTrue(result['ok'], result)

    def test_fake_success_fixed_argv_seed_environment_and_readonly_base(self):
        with patch.object(vm.os, "environ", {"OPENAI_API_KEY": "synthetic-do-not-inherit", "QEMU_AUDIO_DRV": "foreign",
                                               "ENVY_RUNTIME_DARWIN_PS": TEST_DARWIN_PS}):
            result = vm.boot_probe(self.config)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"], "linux-vm-boot-only")
        self.assertTrue(result["noNixInstallYet"])
        self.assertIs(result["receipt"]["nixPresent"], False)
        self.assertEqual(self.root.joinpath("owned").stat().st_mode & 0o777, 0o700)
        calls = self.calls()
        self.assertEqual([c["role"] for c in calls], ["qemu-img", "qemu-img", "qemu-img", "mkisofs", "qemu"])
        fd_path = calls[0]["args"][-1]
        self.assertRegex(fd_path, r"^/dev/fd/[0-9]+$")
        create = calls[1]["args"]
        self.assertEqual(create, ["create", "-f", "qcow2", "-F", "qcow2", "-b", fd_path, create[-1]])
        self.assertRegex(create[-1], r"^/dev/fd/[0-9]+$")
        self.assertEqual(calls[2]["args"][-2], create[-1])
        self.assertEqual(calls[2]["args"][-1], "40G")
        args = calls[-1]["args"]
        for option, value in (("-machine", "pc,accel=tcg"), ("-smp", "2"), ("-m", "4096"),
                              ("-display", "none"), ("-monitor", "none"), ("-nic", "none"),
                              ("-serial", "stdio"), ("-audiodev", "none,id=runtime-audio")):
            self.assertEqual(args[args.index(option) + 1], value)
        for option in ("-nodefaults", "-no-user-config", "-no-reboot"):
            self.assertIn(option, args)
        for forbidden in ("-drive", "-qmp", "-chardev", "-netdev", "-net", "-virtfs", "-fsdev", "-vnc", "-daemonize"):
            self.assertNotIn(forbidden, args)
        nodes = [json.loads(args[i + 1]) for i, arg in enumerate(args) if arg == "-blockdev"]
        self.assertTrue(nodes[0]["backing"]["read-only"])
        self.assertTrue(nodes[0]["backing"]["file"]["read-only"])
        self.assertEqual(nodes[0]["backing"]["file"]["filename"], "/dev/fdset/1")
        registration = args[args.index("-add-fd") + 1]
        self.assertEqual(registration, f"fd={fd_path.rsplit('/', 1)[1]},set=1,opaque=verified-base")
        self.assert_verified_calls(calls, self.base.stat())
        self.assertTrue(nodes[1]["read-only"])
        self.assertEqual(nodes[1]["file"]["filename"], "/dev/fdset/3")
        self.assertEqual(nodes[0]["file"]["filename"], "/dev/fdset/2")
        for call in calls:
            self.assertNotIn("OPENAI_API_KEY", call["env"])
            self.assertNotIn("QEMU_AUDIO_DRV", call["env"])
            self.assertEqual(call["env"]["HOME"], str(self.tools))
            self.assertEqual(call["cwd"], str(self.tools))
            self.assertEqual(call["env"]["TMPDIR"], vm.native_tmp())
            self.assertEqual(call["env"]["XDG_CONFIG_HOME"], str(self.tools))
            self.assertEqual(call["env"]["XDG_CACHE_HOME"], str(self.tools))
        seed = (self.tools / "observations/user-data").read_text()
        self.assertIn("name: envy-runtime", seed)
        self.assertIn("homedir: /home/envy-runtime", seed)
        self.assertIn("NOPASSWD:ALL", seed)
        self.assertIn("command -v nix", seed)
        self.assertIn("uname -m", seed)
        self.assertIn("/dev/ttyS0", seed)
        self.assertNotIn(str(self.root), seed)
        self.assertNotIn("ssh_authorized_keys", seed)
        self.assertNotIn("curl", seed)
        self.assertNotIn("install", seed)
        self.assertEqual(json.loads((self.tools / "observations/meta-data").read_text())["local-hostname"], "envy-runtime")
        self.assert_base_unchanged()

    def assert_verified_calls(self, calls, verified):
        readers = [call for call in calls if call["role"] != "mkisofs"]
        self.assertEqual(len(readers), 4)
        for call in readers:
            self.assertEqual(call["base"], {"device": verified.st_dev, "inode": verified.st_ino,
                                            "sha256": self.config.sha256})
        self.assertNotIn("base", next(call for call in calls if call["role"] == "mkisofs"))

    def replace_base_parent(self):
        original = self.root / "verified-dir"
        self.base.parent.rename(original)
        self.base.parent.mkdir()
        self.base.write_bytes(b"evil replacement: must never be opened by any tool")
        self.base.chmod(0o444)
        return original / self.base.name

    def test_parent_rename_after_hash_keeps_verified_inode_for_all_fake_tools(self):
        verified = self.base.stat()
        original_preflight = vm.preflight
        held = []

        @contextlib.contextmanager
        def swapped_preflight(config, deadline):
            with original_preflight(config, deadline) as prepared:
                held.append(prepared[0])
                self.replace_base_parent()
                yield prepared

        with patch.object(vm, "preflight", swapped_preflight):
            result = vm.boot_probe(self.config)
        self.assertTrue(result["ok"], result)
        self.assert_verified_calls(self.calls(), verified)
        self.assertEqual((self.root / "verified-dir/base.qcow2").read_bytes(), self.base_bytes)
        self.assertEqual((self.root / "verified-dir/base.qcow2").stat().st_mode & 0o777, 0o444)
        self.assertTrue(self.base.read_bytes().startswith(b"evil replacement"))
        with self.assertRaises(OSError):
            os.fstat(held[0])

    def test_parent_rename_after_image_info_keeps_verified_inode_for_boot(self):
        verified = self.base.stat()
        original_validate = vm.validate_image_info

        def swap_after_info(raw):
            original_validate(raw)
            self.replace_base_parent()

        with patch.object(vm, "validate_image_info", swap_after_info):
            result = vm.boot_probe(self.config)
        self.assertTrue(result["ok"], result)
        self.assert_verified_calls(self.calls(), verified)
        self.assertEqual((self.root / "verified-dir/base.qcow2").read_bytes(), self.base_bytes)
        self.assertEqual((self.root / "verified-dir/base.qcow2").stat().st_mode & 0o777, 0o444)
        self.assertTrue(self.base.read_bytes().startswith(b"evil replacement"))

    def test_explicit_descriptor_allowlist_and_close_after_success(self):
        original_spawn = vm.subprocess.Popen
        def checked_spawn(*args, **kwargs):
            if args[0][0] == self.darwin_ps:
                self.assert_query_spawn(args[0], kwargs)
            for fd in kwargs['pass_fds']:
                self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
            return original_spawn(*args, **kwargs)
        with patch.object(vm.subprocess, "Popen", side_effect=checked_spawn) as spawn:
            result = vm.boot_probe(self.config)
        self.assertTrue(result["ok"], result)
        native_calls = [call for call in spawn.call_args_list if call.args[0][0] != self.darwin_ps]
        descriptor = native_calls[0].kwargs["pass_fds"][0]
        passed = [call.kwargs["pass_fds"] for call in native_calls]
        self.assertEqual([len(fds) for fds in passed], [1, 2, 2, 4, 3])
        self.assertEqual(passed[1], passed[2])
        self.assertEqual(passed[4][:2], passed[1])
        self.assertEqual(passed[0], (descriptor,))
        for fd in {fd for fds in passed for fd in fds}:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertTrue(all(call.kwargs["close_fds"] for call in spawn.call_args_list))
        with self.assertRaises(OSError):
            os.fstat(descriptor)
        self.assert_base_unchanged()

    def test_held_descriptor_closes_on_hash_failure_and_hash_cancel(self):
        original_open = vm.os.open
        held = []

        def track_open(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            held.append(descriptor)
            return descriptor

        for cancel in (False, True):
            with self.subTest(cancel=cancel), patch.object(vm.os, "open", side_effect=track_open) as opened:
                if cancel:
                    with patch.object(vm.hashlib, "sha256", side_effect=KeyboardInterrupt):
                        result = vm.boot_probe(self.config)
                else:
                    result = vm.boot_probe(replace(self.config, sha256="0" * 64))
                self.assertEqual(result["error"], "cancelled" if cancel else "sha256-mismatch")
                self.assertEqual(opened.call_count, 1)
            with self.assertRaises(OSError):
                os.fstat(held[-1])
            self.assertFalse((self.root / "owned").exists())
        self.assert_base_unchanged()

    def test_preflight_rejections_never_spawn_or_create_workdir(self):
        mutations = [
            {"base": None}, {"sha256": None}, {"qemu": None}, {"qemu_img": None}, {"mkisofs": None},
            {"base": str(self.root / "missing")}, {"base": str(self.root)},
            {"base": "relative.qcow2"}, {"base": str(self.root / "bad,media=cdrom")},
            {"base": str(self.root / "bad\nfile")}, {"base": str(self.root / "bad:drive")},
            {"workdir": str(self.root / "bad,dir")}, {"workdir": str(self.root / "bad\ndir")},
            {"workdir": str(self.root / "missing/owned")}, {"workdir": "relative"},
            {"qemu": str(self.root / "fake,option")}, {"qemu_img": str(self.root / "bad\nfile")},
            {"qemu": str(self.root / "missing-qemu")}, {"mkisofs": str(self.root / "missing-iso")},
            {"sha256": "0" * 64}, {"sha256": "bad"},
            {"timeout": 0}, {"timeout": 601}, {"timeout": float("nan")}, {"timeout": float("inf")},
            {"timeout": True}, {"output_limit": 1023}, {"output_limit": vm.MAX_OUTPUT + 1},
        ]
        with patch.object(vm.subprocess, "Popen") as spawn:
            for mutation in mutations:
                with self.subTest(mutation=mutation):
                    result = vm.boot_probe(replace(self.config, **mutation))
                    self.assertFalse(result["ok"])
                    self.assertEqual(result["status"], "unavailable")
                    self.assertEqual(result["commands"], [])
                    self.assertFalse((self.root / "owned").exists())
            spawn.assert_not_called()
        self.assert_base_unchanged()

    def test_existing_directory_symlink_and_writable_base_never_spawn(self):
        owned = self.root / "owned"
        owned.mkdir()
        (owned / "keep").write_text("retained")
        linked = self.root / "linked.qcow2"
        linked.symlink_to(self.base)
        with patch.object(vm.subprocess, "Popen") as spawn:
            self.assertFalse(vm.boot_probe(self.config)["ok"])
            self.assertEqual((owned / "keep").read_text(), "retained")
            cfg = replace(self.config, workdir=str(self.root / "another"), base=str(linked))
            self.assertFalse(vm.boot_probe(cfg)["ok"])
            self.base.chmod(0o644)
            self.assertFalse(vm.boot_probe(replace(cfg, base=str(self.base)))["ok"])
            spawn.assert_not_called()
        self.assertFalse((self.root / "another").exists())

    def test_image_metadata_rejected_before_overlay_or_boot(self):
        for index, info in enumerate((
            {"format": "raw", "virtual-size": 1},
            {"format": "qcow2", "virtual-size": 1, "backing-filename": "/foreign.qcow2"},
            {"format": "qcow2", "virtual-size": 1, "full-backing-filename": "/foreign.qcow2"},
            {"format": "qcow2", "virtual-size": 1, "format-specific": {"data": {"data-file": "/foreign"}}},
            {"format": "qcow2", "virtual-size": 1, "encrypted": True},
            {"format": "qcow2", "virtual-size": vm.DISK_SIZE + 1},
            {"format": "qcow2", "virtual-size": True},
        )):
            with self.subTest(info=info):
                self.configure(info=info)
                result = vm.boot_probe(replace(self.config, workdir=str(self.root / ("badinfo" + str(index)))))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failure")
                self.assertEqual(len(result["commands"]), 1)
                self.assertEqual(result["commands"][0]["stage"], "image-info")
        self.assert_base_unchanged()

    def test_exit_zero_is_insufficient_and_receipts_do_not_prove_install(self):
        for mode in ("none", "wrong-token", "wrong-arch", "nix", "zero", "duplicate", "receipt-error", "exit-error"):
            with self.subTest(mode=mode):
                self.configure(mode=mode)
                result = vm.boot_probe(replace(self.config, workdir=str(self.root / mode)))
                self.assertFalse(result["ok"])
                self.assertEqual(result["status"], "failure")
                self.assertIsNone(result["receipt"])
                self.assertTrue(result["noNixInstallYet"])
                self.assertNotIn("synthetic private diagnostic", json.dumps(result))
                self.assertTrue(Path(result["commands"][-1]["stderr"]).is_file())
        self.assert_base_unchanged()

    def assert_owned_stopped(self, result, *, child_expected=True):
        record = result['commands'][-1]
        owned = [p for p in self.owned_leaders if p.pid == record['pid']]
        self.assertEqual(len(owned), 1)
        process = owned[0]
        self.assertIsNotNone(process.returncode, 'own Popen leader was not reaped')
        self.assertEqual(record['returncode'], process.returncode)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)
        if not child_expected:
            return
        ready = self.tools / 'observations/child-ready.json'
        self.assertTrue(ready.exists(), 'known child handshake never completed')
        row = json.loads(ready.read_text())
        self.assertEqual(row['stage'], record['stage'])
        self.assertEqual(row['leader'], {'pid': process.pid, 'ppid': os.getpid(), 'pgid': process.pid})
        self.assertEqual(row['child']['ppid'], process.pid)
        self.assertEqual(row['child']['pgid'], process.pid)
        self.assertGreater(row['child']['pid'], 0)
        self.assertNotEqual(row['child']['pid'], process.pid)
        info = os.fstat(self.lease_fd)
        self.assertTrue(stat.S_ISREG(info.st_mode))
        self.assertEqual(row['lease'], [info.st_dev, info.st_ino])
        deadline = REAL_MONOTONIC() + 3
        while True:
            try:
                fcntl.flock(self.lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if REAL_MONOTONIC() >= deadline:
                    self.fail('known child still holds its exact lease inode')
                time.sleep(0.005)
        fcntl.flock(self.lease_fd, fcntl.LOCK_UN)
        # This proves the known child no longer holds its lifetime FD.
        # It makes no claim that a zombie or the whole group is physically gone.

    @contextlib.contextmanager
    def timeout_after_handshake(self, module=vm):
        started = REAL_MONOTONIC()
        deadline = started + 10
        def monotonic():
            now = REAL_MONOTONIC()
            if (self.tools / 'observations/child-ready.json').exists():
                return deadline + 1
            if now - started > 5:
                raise AssertionError('bounded native fixture startup failed')
            return started
        with patch.object(module, 'time', SimpleNamespace(monotonic=monotonic)):
            yield

    def test_child_publishes_pid_and_held_lock_before_ready(self):
        self.configure(mode='hang')
        observations = self.tools / 'observations'
        observations.mkdir()
        ready = observations / 'child-ready.json'
        leader = {'pid': 456789, 'ppid': 456788, 'pgid': 456789}
        code = next(node.value.value for node in ast.walk(ast.parse(FAKE_TOOL))
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                    and any(isinstance(target, ast.Name) and target.id == 'code' for target in node.targets))
        original_open, original_replace = os.open, Path.replace
        descriptors, published = [], []
        def tracked_open(*args, **kwargs):
            descriptor = original_open(*args, **kwargs)
            descriptors.append(descriptor)
            return descriptor
        def publish(path, target):
            if target == ready:
                row = json.loads(path.read_text())
                self.assertEqual((observations / 'child.pid').read_text(), str(row['child']['pid']))
                self.assertEqual(row['stage'], 'qemu')
                self.assertEqual(row['leader'], leader)
                self.assertEqual(row['child'], {'pid': 456790, 'ppid': leader['pid'], 'pgid': leader['pgid']})
                info = os.fstat(self.lease_fd)
                self.assertEqual(row['lease'], [info.st_dev, info.st_ino])
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(self.lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                published.append(True)
            return original_replace(path, target)
        try:
            with patch.object(os, 'open', side_effect=tracked_open), \
                    patch.object(os, 'getpid', return_value=456790), \
                    patch.object(os, 'getppid', return_value=leader['pid']), \
                    patch.object(os, 'getpgrp', return_value=leader['pgid']), \
                    patch.object(sys, 'argv', ['child', str(self.tools / 'child.lease'),
                                             str(ready), json.dumps(leader), 'qemu']), \
                    patch.object(Path, 'replace', publish), patch.object(time, 'sleep'):
                exec(compile(code, '<synthetic-child>', 'exec'), {})
            self.assertEqual(published, [True])
        finally:
            for descriptor in descriptors:
                os.close(descriptor)
        fcntl.flock(self.lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(self.lease_fd, fcntl.LOCK_UN)

    def test_timeout_reaps_own_group_and_preserves_unrelated_process_and_base(self):
        self.configure(mode="hang")
        unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
        try:
            started = time.monotonic()
            with self.timeout_after_handshake():
                result = vm.boot_probe(replace(self.config, timeout=10))
            self.assertEqual(result['commands'][-1]['stage'], 'qemu')
            self.assertEqual(result["error"], "timeout", result)
            self.assertLess(time.monotonic() - started, 5)
            self.assertIsNone(os.waitid(os.P_PID, unrelated.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT))
            self.assert_owned_stopped(result)
            self.assertTrue((self.tools / "observations/child.pid").exists())
            self.assert_base_unchanged()
        finally:
            lease = vm._ProcessLease(unrelated, vm.wait_flags())
            try:
                vm.Runner.stop(lease)
            finally:
                lease.reap()

    def test_native_direct_leader_exit_and_live_timeout_with_known_child(self):
        for mode in ('hang', 'leader-exit', 'closed-stdio', 'child-exit'):
            with self.subTest(mode=mode):
                self.configure(mode=mode)
                setup = FAKE_TOOL[:FAKE_TOOL.index('call = ')]
                child_function = FAKE_TOOL[FAKE_TOOL.index('def start_child():'):FAKE_TOOL.index("if role == 'qemu-img' and settings.get('prepare_cancel')")]
                body = setup + child_function + "start_child()\n"
                if mode == 'hang':
                    body += "time.sleep(60)\n"
                if mode == 'child-exit':
                    body = body.removesuffix("start_child()\n") + "assert start_child().wait(timeout=3) == 0\n"
                (self.tools / 'qemu').write_text('#!' + sys.executable + '\n' + body)
                work = self.root / mode
                work.mkdir()
                with vm.pinned_work(work) as (fd, _):
                    runner = vm.Runner(work, [str(self.tools / 'qemu')],
                                       REAL_MONOTONIC() + 10, 65536, directory_fd=fd, darwin_ps=TEST_DARWIN_PS)
                    if mode == 'hang':
                        with self.timeout_after_handshake(), self.assertRaisesRegex(vm.ProbeError, '^timeout$'):
                            runner.run('qemu', [str(self.tools / 'qemu')])
                    else:
                        self.assertEqual(runner.run('qemu', [str(self.tools / 'qemu')]), b'')
                self.assert_owned_stopped({'commands': runner.commands})
                self.assertEqual(runner.commands[-1]['returncode'], -9 if mode == 'hang' else 0)
                if mode == 'child-exit' and sys.platform == 'darwin':
                    self.assertTrue(runner.commands[-1]['cleanupEvidence']['allObservedMembersZombie'])
        self.assert_base_unchanged()

    def test_native_group_query_rejects_known_live_locked_child_before_owned_stop(self):
        self.configure(mode='leader-exit')
        setup = FAKE_TOOL[:FAKE_TOOL.index('call = ')]
        child_function = FAKE_TOOL[FAKE_TOOL.index('def start_child():'):FAKE_TOOL.index("if role == 'qemu-img' and settings.get('prepare_cancel')")]
        (self.tools / 'qemu').write_text('#!' + sys.executable + '\n' + setup + child_function + 'start_child()\n')
        work = self.root / 'live-query'
        work.mkdir()
        original_stop = vm.Runner.stop
        checked = []
        def check_then_stop(lease):
            if not checked:
                checked.append(True)
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(self.lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                try:
                    if sys.platform == 'darwin':
                        with self.assertRaisesRegex(vm.ProbeError, 'group-query-shape'):
                            lease._darwin_zombies()
                        self.assertIsNone(lease.cleanup_evidence)
                finally:
                    original_stop(lease)
            else:
                original_stop(lease)
        with vm.pinned_work(work) as (fd, _):
            runner = vm.Runner(work, [str(self.tools / 'qemu')], REAL_MONOTONIC() + 10,
                               65536, directory_fd=fd, darwin_ps=TEST_DARWIN_PS)
            with patch.object(vm.Runner, 'stop', staticmethod(check_then_stop)):
                self.assertEqual(runner.run('qemu', [str(self.tools / 'qemu')]), b'')
        self.assertEqual(checked, [True])
        self.assert_owned_stopped({'commands': runner.commands})
        self.assert_base_unchanged()

    def test_output_cap_kills_at_runtime_and_keeps_bounded_logs(self):
        self.configure(mode="flood")
        started = time.monotonic()
        result = vm.boot_probe(replace(self.config, output_limit=4096))
        self.assertEqual(result["error"], "output-limit", result)
        self.assertLess(time.monotonic() - started, 5)
        logs = list((self.root / "owned").glob("*.log"))
        self.assertLessEqual(sum(log.stat().st_size for log in logs), 4096)
        self.assert_owned_stopped(result)
        self.assert_base_unchanged()

    def test_keyboard_cancel_kills_group_and_returns_json_failure(self):
        self.configure(mode="cancel")
        started = time.monotonic()
        result = vm.boot_probe(self.config)
        self.assertEqual(result["error"], "cancelled", result)
        self.assertLess(time.monotonic() - started, 5)
        self.assert_owned_stopped(result)
        self.assert_base_unchanged()

    def test_prepare_cancel_kills_group_and_closes_held_base(self):
        original_spawn = vm.subprocess.Popen
        for stage in ("info", "create", "resize"):
            with self.subTest(stage=stage):
                self.configure(prepare_cancel=stage)
                with patch.object(vm.subprocess, "Popen", wraps=original_spawn) as spawn:
                    result = vm.boot_probe(replace(self.config, workdir=str(self.root / stage)))
                self.assertEqual(result["error"], "cancelled", result)
                self.assert_owned_stopped(result)
                descriptor = spawn.call_args_list[0].kwargs["pass_fds"][0]
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
                self.assert_base_unchanged()

    def test_missing_api_and_cli_inputs_are_unavailable_without_defaults(self):
        with patch.object(vm.subprocess, "Popen") as spawn:
            result = vm.boot_probe(vm.BootConfig(workdir=self.config.workdir))
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["error"], "missing-arguments")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = vm.main(["boot-probe", "--workdir", self.config.workdir])
            self.assertEqual(code, 1)
            result = json.loads(output.getvalue())
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["error"], "missing-arguments")
            spawn.assert_not_called()
        self.assertFalse((self.root / "owned").exists())

    def test_cli_has_no_implicit_vm_and_invalid_arguments_are_json(self):
        for args in ([], ["--timeout", "1"], ["boot-probe"], ["unknown"],
                     ["boot-probe", "--workdir", str(self.root / "owned"), "--timeout", "bad"]):
            with self.subTest(args=args), patch.object(vm.subprocess, "Popen") as spawn:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = vm.main(args)
                result = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertEqual(result["status"], "unavailable")
                spawn.assert_not_called()
        self.assertFalse((self.root / "owned").exists())

    def test_cli_success_is_parseable_and_explicit(self):
        args = ["boot-probe", "--workdir", self.config.workdir, "--base", self.config.base,
                "--sha256", self.config.sha256, "--qemu", self.config.qemu,
                "--qemu-img", self.config.qemu_img, "--mkisofs", self.config.mkisofs]
        if TEST_DARWIN_PS is not None:
            args.extend(["--darwin-ps", TEST_DARWIN_PS])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = vm.main(args)
        self.assertEqual(code, 0, output.getvalue())
        self.assertTrue(json.loads(output.getvalue())["ok"])


if __name__ == "__main__":
    unittest.main()
