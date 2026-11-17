"""Synthetic guest-program tests. No VM, Nix, host setup or network execution."""
import copy
import errno
from contextlib import ExitStack
import io
import json
from urllib.parse import parse_qs, urlsplit
import os
import stat
import tempfile
from pathlib import Path
import sys
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import guest
import acceptance
import test_acceptance as receipt_fixtures
from test_acceptance import store, sri


EMIT_SERIAL = guest.emit_serial
CHECKED = guest.checked
TOKEN = 'b' * 64
CONFIG = {'token': TOKEN, 'metadataSha256': 'c' * 64, 'metadataSize': 100,
          'installTimeout': 1200, 'fullTimeout': 7200, 'transport': 'nix-export'}
ROOT_DEVICE = os.makedev(241, 37)
ROOT_IDENTITY = '241:37'
ROOT_MOUNT = '24 1 241:37 / / rw,relatime - ext4 /dev/vda1 rw\n'
SEED_MOUNT = '25 24 11:0 / /run/envy-runtime-install-seed ro,nosuid,nodev,noexec - iso9660 /dev/sr0 ro\n'


def metadata():
    return {'schemaVersion': 1,
            'source': {'path': store('source'), 'narHash': sri(b'source'),
                       'lockSha256': '1' * 64, 'manifestSha256': '2' * 64},
            'snapshot': {'size': 10240, 'sha256': '3' * 64},
            'preparation': {'format': 'nix-export', 'signatureScope': guest.SIGNATURE_SCOPE,
                'artifact': {'size': 100, 'sha256': '4' * 64},
                'objects': [{'path': store('source'), 'narHash': sri(b'source'), 'narSize': 100,
                             'references': [store('source')]}]}}


def network_metadata():
    row = metadata()
    del row['preparation']
    return {**row, 'transport': 'guest-fetch', 'sourceNarSize': 100}


def network_config(meta=None):
    meta = network_metadata() if meta is None else meta
    raw = (json.dumps(meta, sort_keys=True) + '\n').encode()
    return {**CONFIG, 'transport': 'guest-fetch', 'metadataSha256': guest.sha(raw), 'metadataSize': len(raw)}


class Isolated(unittest.TestCase):
    def enterContext(self, context):
        return self.stack.enter_context(context)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Install prohibitions before exercising any execution helper. Every
        # test opts in to specific fake boundaries, never real subprocesses.
        for name in ('subprocess.Popen', 'pty.fork', 'os.execve'):
            target = self.enterContext(patch('guest.' + name, side_effect=AssertionError('host execution forbidden')))
            setattr(self, name.replace('.', '_'), target)
        # No test may observe, reap or signal a real host process.
        self.waitid = self.enterContext(patch.object(guest.os, 'waitid', create=True,
            side_effect=AssertionError('host waitid forbidden')))
        for name in ('waitpid', 'killpg', 'kill', 'wait'):
            self.enterContext(patch.object(guest.os, name,
                side_effect=AssertionError('host wait/signal forbidden')))
        for name, value in {'P_PID': 1, 'WEXITED': 4, 'WNOHANG': 1, 'WNOWAIT': 0x1000000,
                            'CLD_EXITED': 1, 'CLD_KILLED': 2, 'CLD_DUMPED': 3}.items():
            self.enterContext(patch.object(guest.os, name, value, create=True))
        self.enterContext(patch.object(guest, 'DEADLINE', 1000))
        self.enterContext(patch.object(guest.time, 'monotonic', return_value=1))
        self.enterContext(patch.object(guest, 'COMMAND_FACTS', []))
        self.enterContext(patch.object(guest, 'ROOT_PHASE', 'root-stages'))
        self.enterContext(patch.object(guest, 'LAST_NIX_FAILURE', 'unknown'))
        self.serial = self.enterContext(patch.object(guest, 'emit_serial'))
        # Existing lifecycle fakes stop at this new native boundary. Dedicated
        # prefetch tests below exercise it with synthetic lock bytes only.
        self.prefetch_patch = patch.object(guest, 'prefetch_github_inputs')
        if not getattr(self, 'REAL_PREFETCH', False):
            self.prefetch = self.enterContext(self.prefetch_patch)
        real_open, real_stat, real_mkdir = os.open, os.stat, os.mkdir
        def isolated_open(path, *args, **kwargs):
            if (os.fsdecode(path) == '/run' or os.fsdecode(path).startswith('/run/')
                    or (kwargs.get('dir_fd') is not None and os.fsdecode(path) in
                        ('run', 'systemd', 'system', 'nix-daemon.service.d', 'envy-runtime-ca.conf'))
                    or (os.fsdecode(path) == '/' and args and args[0] & os.O_DIRECTORY)):
                raise AssertionError('host runtime IO forbidden')
            if os.fspath(path) in ('/dev/ttyS0', b'/dev/ttyS0',
                                   guest.SYSTEM_CA_BUNDLE, guest.SYSTEM_CA_BUNDLE.encode()):
                raise AssertionError('host serial/CA forbidden')
            return real_open(path, *args, **kwargs)
        def isolated_stat(path, *args, **kwargs):
            if not isinstance(path, int) and os.fsdecode(path).startswith('/run/'):
                raise AssertionError('host runtime metadata forbidden')
            if not isinstance(path, int) and os.fsdecode(path) == guest.SYSTEM_CA_BUNDLE:
                raise AssertionError('host CA metadata forbidden')
            return real_stat(path, *args, **kwargs)
        def isolated_mkdir(path, *args, **kwargs):
            if (os.fsdecode(path) == '/run' or os.fsdecode(path).startswith('/run/')
                    or (kwargs.get('dir_fd') is not None and os.fsdecode(path) == 'nix-daemon.service.d')):
                raise AssertionError('host runtime mutation forbidden')
            return real_mkdir(path, *args, **kwargs)
        self.enterContext(patch.object(guest.os, 'mkdir', side_effect=isolated_mkdir))
        self.enterContext(patch.object(guest.os, 'open', side_effect=isolated_open))
        self.enterContext(patch.object(guest.os, 'stat', side_effect=isolated_stat))


class RenderingTests(Isolated):
    def test_rendered_program_is_self_contained_closed_data_and_official_seed(self):
        meta = metadata()
        program = guest.guest_program(TOKEN, meta)
        namespace = {'__name__': 'rendered_guest'}
        exec(compile(program, '<inert guest rendering>', 'exec'), namespace)
        rendered = namespace['RUN_CONFIG']
        self.assertEqual(rendered['metadataSha256'], guest.sha((json.dumps(meta, sort_keys=True) + '\n').encode()))
        namespace['validate_config'](rendered)
        self.assertNotIn('ENVY_DEV_SHELL', namespace['environment']())
        data, instance = guest.seed_data(TOKEN, meta)
        self.assertIn('/usr/local/lib/envy-runtime-install-probe.py', data)
        self.assertTrue(data.endswith('runcmd:\n  - [ /usr/bin/python3, /usr/local/lib/envy-runtime-guest.py ]\n'))
        self.assertIn('permissions: \'0444\'', data)
        self.assertEqual(json.loads(instance)['instance-id'], 'envy-runtime-' + TOKEN)

    def test_rejects_commands_paths_wrong_nonce_shapes_and_nonfinite_timeouts(self):
        for key, value in (('token', 'wrong'), ('metadataSize', True), ('installTimeout', float('nan')),
                           ('fullTimeout', 7201), ('guestCommand', 'sh'), ('hostMount', '/Users'),
                           ('rootDevice', '/dev/vdb1'), ('root_identity', ROOT_IDENTITY)):
            config = {**CONFIG, key: value}
            with self.subTest(key=key), self.assertRaises(guest.GuestFailure):
                guest.validate_config(config)
        for action in ('guestCommand', 'network', 'rawSecret'):
            meta = metadata()
            meta[action] = 'SYNTHETIC-PRIVATE'
            with self.assertRaises(guest.GuestFailure) as caught:
                guest.guest_program(TOKEN, meta)
            self.assertNotIn('PRIVATE', str(caught.exception))

    def test_no_entry_calls_io_after_guard_rejection(self):
        for entry in (guest.root_stages, guest.user_stages):
            with self.subTest(entry=entry.__name__), patch.object(guest, 'guard', side_effect=guest.GuestFailure('guard')), \
                 patch.object(guest.os, 'open', side_effect=AssertionError('IO before guard')), \
                 patch.object(guest, 'command', side_effect=AssertionError('command before guard')):
                with self.assertRaises(guest.GuestFailure):
                    entry(CONFIG)

    def test_guard_wrong_nonce_stops_before_mount_inventory_and_mutation(self):
        with patch.object(guest.sys, 'platform', 'linux'), patch.object(guest.platform, 'machine', return_value='x86_64'), \
             patch.object(guest.os, 'getuid', return_value=0), patch.object(guest.os, 'geteuid', return_value=0), \
             patch.object(guest, 'private_read', return_value=b'wrong\n'), \
             patch.object(guest.os, 'stat', side_effect=AssertionError('metadata after wrong nonce')), \
             patch.object(Path, 'read_text', side_effect=AssertionError('mount read after wrong nonce')):
            with self.assertRaisesRegex(guest.GuestFailure, 'guest-nonce'):
                guest.guard(CONFIG, root=True)


class MountParserTests(Isolated):
    def parse(self, raw, *, mounted=True):
        guest.mounts(raw, mounted=mounted, root_identity=ROOT_IDENTITY)

    def test_source_aliases_require_same_physical_root_identity(self):
        for alias in ('/dev/vda1', '/dev/root', 'UUID=synthetic-root', '/dev/disk/by-uuid/synthetic-root'):
            with self.subTest(alias=alias):
                self.parse(ROOT_MOUNT.replace('/dev/vda1', alias) + SEED_MOUNT)

    def test_root_identity_filesystem_subtree_and_cardinality_fail_closed(self):
        for root in ('', ROOT_MOUNT.replace('241:37', '241:38'),
                     ROOT_MOUNT.replace('ext4', 'xfs'), ROOT_MOUNT.replace(' / / ', ' /subtree / '),
                     ROOT_MOUNT * 2, ROOT_MOUNT + ROOT_MOUNT.replace('241:37', '241:38')):
            with self.subTest(root=root), self.assertRaisesRegex(guest.GuestFailure, '^guest-root-mount$'):
                self.parse(root + SEED_MOUNT)

    def test_all_host_filesystems_and_host_paths_still_refuse(self):
        for fs in ('9p', 'virtiofs', 'nfs', 'nfs4', 'cifs', 'fuse.sshfs'):
            with self.subTest(fs=fs), self.assertRaisesRegex(guest.GuestFailure, '^guest-host-mount$'):
                self.parse(ROOT_MOUNT + SEED_MOUNT + f'26 24 0:4 / /mnt/share rw - {fs} share rw\n')
        for path in ('/Users/share', '/host/share', '/mnt/host/share'):
            with self.subTest(path=path), self.assertRaisesRegex(guest.GuestFailure, '^guest-host-mount$'):
                self.parse(ROOT_MOUNT + SEED_MOUNT + f'26 24 0:4 / {path} rw - ext4 /dev/vdb1 rw\n')

    def test_seed_filesystem_device_and_each_readonly_restriction_remain_required(self):
        seeds = ['', SEED_MOUNT.replace('iso9660', 'ext4'), SEED_MOUNT.replace('/dev/sr0', '/dev/sr1')]
        seeds += [SEED_MOUNT.replace('ro,nosuid,nodev,noexec', ','.join(
            item for item in ('ro', 'nosuid', 'nodev', 'noexec') if item != missing))
            for missing in ('ro', 'nosuid', 'nodev', 'noexec')]
        for seed in seeds:
            with self.subTest(seed=seed), self.assertRaisesRegex(guest.GuestFailure, '^seed-not-readonly-iso$'):
                self.parse(ROOT_MOUNT + seed)
        self.parse(ROOT_MOUNT, mounted=False)


class PhysicalRootGuardTests(Isolated):
    def boundaries(self, *, root=True, block=None, root_info=None, raw=None, stat_error=None):
        block = SimpleNamespace(st_mode=stat.S_IFBLK | 0o600, st_rdev=ROOT_DEVICE) if block is None else block
        root_info = SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_dev=ROOT_DEVICE) if root_info is None else root_info
        self.enterContext(patch.object(guest.sys, 'platform', 'linux'))
        self.enterContext(patch.object(guest.platform, 'machine', return_value='x86_64'))
        for name in ('getuid', 'geteuid'):
            self.enterContext(patch.object(guest.os, name, return_value=0 if root else 1000))
        self.marker = self.enterContext(patch.object(guest, 'private_read', return_value=(TOKEN + '\n').encode()))
        def metadata(path, *, follow_symlinks=True):
            name = str(path)
            if name in ('/dev/vda1', '/'):
                self.assertFalse(follow_symlinks)
                if stat_error is not None:
                    raise stat_error
                return block if name == '/dev/vda1' else root_info
            self.assertFalse(root)
            self.assertTrue(follow_symlinks)
            self.assertIn(name, ('/run/user/1000', '/run/user/1000/bus'))
            return SimpleNamespace(st_mode=stat.S_IFSOCK | 0o600) if name.endswith('/bus') else SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700, st_uid=1000)
        self.metadata = self.enterContext(patch.object(guest.os, 'stat', side_effect=metadata))
        def mountinfo(path):
            self.assertEqual(self.metadata.call_count, 2)
            self.assertEqual(str(path), '/proc/self/mountinfo')
            return ROOT_MOUNT + SEED_MOUNT if raw is None else raw
        self.proc = self.enterContext(patch.object(Path, 'read_text', autospec=True, side_effect=mountinfo))
        self.network = self.enterContext(patch.object(guest, 'network_guard'))
        self.enterContext(patch.object(guest.os, 'open', side_effect=AssertionError('block/file open forbidden')))
        self.enterContext(patch.object(guest.pwd, 'getpwuid', return_value=SimpleNamespace(
            pw_name='envy-runtime', pw_dir=str(guest.HOME))))
        self.enterContext(patch.dict(guest.os.environ, {'HOME': str(guest.HOME), 'XDG_RUNTIME_DIR': '/run/user/1000'}, clear=True))

    def test_root_and_user_guards_bind_fixed_metadata_before_proc_and_network(self):
        for root in (True, False):
            with self.subTest(root=root), ExitStack() as stack:
                previous = self.stack
                self.stack = stack
                try:
                    self.boundaries(root=root, raw=ROOT_MOUNT.replace('/dev/vda1', '/dev/root') + SEED_MOUNT)
                    guest.guard(CONFIG, root=root)
                    self.assertEqual(self.metadata.call_args_list[0].args, ('/dev/vda1',))
                    self.assertEqual(self.metadata.call_args_list[1].args, ('/',))
                    self.proc.assert_called_once()
                    self.network.assert_called_once_with('nix-export')
                finally:
                    self.stack = previous

    def test_wrong_root_device_nonblock_and_symlink_metadata_refuse_before_proc(self):
        cases = ({'root_info': SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=os.makedev(241, 38))},
                 {'block': SimpleNamespace(st_mode=stat.S_IFREG, st_rdev=ROOT_DEVICE)},
                 {'block': SimpleNamespace(st_mode=stat.S_IFLNK, st_rdev=ROOT_DEVICE)},
                 {'root_info': SimpleNamespace(st_mode=stat.S_IFREG, st_dev=ROOT_DEVICE)},
                 {'root_info': SimpleNamespace(st_mode=stat.S_IFLNK, st_dev=ROOT_DEVICE)})
        for case in cases:
            with self.subTest(case=case), ExitStack() as stack:
                previous = self.stack
                self.stack = stack
                try:
                    self.boundaries(**case)
                    with self.assertRaisesRegex(guest.GuestFailure, '^guest-root-mount$'):
                        guest.guard(CONFIG, root=True)
                    self.proc.assert_not_called()
                    self.network.assert_not_called()
                finally:
                    self.stack = previous

    def test_initial_platform_uid_and_nonce_refusals_precede_all_device_metadata(self):
        self.boundaries()
        for boundary, value, code in (('platform', 'darwin', 'guest-platform'),
                                      ('uid', 1000, 'guest-uid'), ('nonce', b'wrong', 'guest-nonce')):
            target, name = (guest.sys, 'platform') if boundary == 'platform' else (
                (guest.os, 'getuid') if boundary == 'uid' else (guest, 'private_read'))
            options = {'new': value} if boundary == 'platform' else {'return_value': value}
            with self.subTest(boundary=boundary), patch.object(target, name, **options), \
                 self.assertRaisesRegex(guest.GuestFailure, '^' + code + '$'):
                guest.guard(CONFIG, root=True)
        self.metadata.assert_not_called()
        self.proc.assert_not_called()
        self.network.assert_not_called()

    def test_mismatched_mount_identity_aborts_with_static_code_before_any_stage(self):
        self.boundaries(raw=ROOT_MOUNT.replace('241:37', '241:38') + SEED_MOUNT)
        with patch.object(guest.sys, 'argv', [guest.SCRIPT]), patch.object(guest.signal, 'signal'), \
             patch.object(guest, 'root_stages') as stages, patch.object(guest, 'checked') as checked, \
             patch.object(guest, 'command') as command, patch.object(guest, 'write_new') as write, \
             patch.object(Path, 'mkdir') as mkdir, patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as output:
            self.assertEqual(guest.seed_entrypoint(CONFIG), 1)
        self.assertEqual(output.getvalue().splitlines(), [guest.ABORT_PREFIX + json.dumps(
            {'schemaVersion': 1, 'token': TOKEN, 'error': 'guest-root-mount'}, sort_keys=True)])
        for mocked in (self.network, stages, checked, command, write, mkdir):
            mocked.assert_not_called()

    def test_device_metadata_oserror_aborts_with_unknown_and_no_stage_or_cleanup(self):
        self.boundaries(stat_error=OSError('/private/SYNTHETIC-DEVICE-ERROR'))
        with patch.object(guest.sys, 'argv', [guest.SCRIPT]), patch.object(guest.signal, 'signal'), \
             patch.object(guest, 'root_stages') as stages, patch.object(guest, 'checked') as checked, \
             patch.object(guest, 'command') as command, patch.object(guest, 'write_new') as write, \
             patch.object(Path, 'mkdir') as mkdir, patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as output:
            self.assertEqual(guest.seed_entrypoint(CONFIG), 1)
        self.assertEqual(output.getvalue().splitlines(), [guest.ABORT_PREFIX + json.dumps(
            {'schemaVersion': 1, 'token': TOKEN, 'error': 'unknown'}, sort_keys=True)])
        for mocked in (self.proc, self.network, stages, checked, command, write, mkdir):
            mocked.assert_not_called()


class FileStabilityTests(Isolated):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory(prefix='envy-guest-read-')
        self.addCleanup(temporary.cleanup)
        self.seed = Path(temporary.name)
        self.path = self.seed / 'payload'
        self.data = b'synthetic marker and payload content\n'
        self.path.write_bytes(self.data)
        self.path.chmod(0o444)
        self.info = os.stat(self.path)

    def read(self, payload, changes):
        # Read real bytes from a real FD. Only the before/after stat observation
        # is synthetic, to deterministically model Linux relatime on Darwin.
        fields = {name: getattr(self.info, name) for name in dir(self.info) if name.startswith('st_')}
        fields['st_uid'] = 0
        before = SimpleNamespace(**fields)
        after = SimpleNamespace(**{**fields, **changes})
        with patch.object(guest.os, 'fstat', side_effect=[before, after]) as observed, \
             patch.object(guest, 'SEED', self.seed):
            if payload:
                fd = guest.payload_fd('payload', {'size': len(self.data), 'sha256': guest.sha(self.data)})
                try:
                    self.assertEqual(os.read(fd, len(self.data) + 1), self.data)
                finally:
                    os.close(fd)
            else:
                self.assertEqual(guest.private_read(self.path, 1024, owner=0, mode=0o444), self.data)
        self.assertEqual(observed.call_count, 2)

    def test_real_reads_allow_atime_and_unstable_statistics_only(self):
        observations = ({'st_atime': self.info.st_atime + 10,
                         'st_atime_ns': self.info.st_atime_ns + 10000000000},
                        {'st_blocks': self.info.st_blocks + 1})
        for payload in (False, True):
            for changes in observations:
                with self.subTest(payload=payload, changes=changes):
                    self.read(payload, changes)

    def test_both_reads_reject_each_stable_security_field_change(self):
        fields = ('st_dev', 'st_ino', 'st_mode', 'st_nlink', 'st_uid', 'st_gid',
                  'st_size', 'st_mtime_ns', 'st_ctime_ns')
        for payload in (False, True):
            for field in fields:
                value = 0 if field == 'st_uid' else getattr(self.info, field)
                with self.subTest(payload=payload, field=field), self.assertRaises(guest.GuestFailure):
                    self.read(payload, {field: value + 1})

    def test_actual_content_mismatch_symlinks_links_and_limits_still_refuse(self):
        with patch.object(guest, 'SEED', self.seed), self.assertRaisesRegex(guest.GuestFailure, 'payload-digest'):
            with patch.object(guest.os, 'fstat', return_value=SimpleNamespace(
                    st_mode=self.info.st_mode, st_uid=0, st_size=len(self.data))):
                guest.payload_fd('payload', {'size': len(self.data), 'sha256': guest.sha(b'other content')})
        with self.assertRaises(guest.GuestFailure):
            guest.private_read(self.path, len(self.data) - 1)
        with self.assertRaises(guest.GuestFailure):
            guest.private_read(self.path, 1024, owner=self.info.st_uid + 1)
        with self.assertRaises(guest.GuestFailure):
            guest.private_read(self.path, 1024, mode=0o600)
        link = self.seed / 'link'
        link.symlink_to(self.path)
        with self.assertRaises(OSError):
            guest.private_read(link, 1024)
        link.unlink()
        os.link(self.path, link)
        with self.assertRaises(guest.GuestFailure):
            guest.private_read(self.path, 1024)


class EntrypointAbortTests(Isolated):
    def test_guard_failure_prints_bound_safe_abort_without_any_mutation_or_cleanup(self):
        for args in ([], ['user'], ['lifecycle']):
            for error in ('file-changed', 'guest-platform', '/private/SYNTHETIC-SECRET',
                          'unexpected-code', 'a' * 101):
                with self.subTest(args=args, error=error), \
                     patch.object(guest.sys, 'argv', [guest.SCRIPT, *args]), \
                     patch.object(guest.signal, 'signal'), \
                     patch.object(guest, 'guard', side_effect=guest.GuestFailure(error)), \
                     patch.object(guest, 'root_stages') as root, patch.object(guest, 'user_stages') as user, \
                     patch.object(guest, 'checked') as checked, patch.object(guest, 'command') as command, \
                     patch.object(guest, 'write_new') as write, patch.object(guest.os, 'open') as opened, \
                     patch.object(Path, 'mkdir') as mkdir, \
                     patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as output:
                    self.assertEqual(guest.seed_entrypoint(CONFIG), 1)
                self.serial.assert_not_called()
                for mocked in (root, user, checked, command, write, opened, mkdir):
                    mocked.assert_not_called()
                lines = output.getvalue().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith('ENVY_RUNTIME_GUEST_ABORT '))
                expected = error if error in ('file-changed', 'guest-platform') else 'unknown'
                self.assertEqual(json.loads(lines[0].split(' ', 1)[1]),
                                 {'schemaVersion': 1, 'token': TOKEN, 'error': expected})

    def test_missing_or_symlink_marker_aborts_without_cleanup_or_path_disclosure(self):
        with tempfile.TemporaryDirectory(prefix='envy-guard-marker-') as directory:
            root = Path(directory)
            target = root / 'synthetic-private-marker'
            target.write_text('SYNTHETIC-DO-NOT-READ')
            link = root / 'linked-marker'
            link.symlink_to(target)
            for marker in (root / 'missing-marker', link):
                with (
                    self.subTest(marker=marker.name),
                    patch.object(guest, 'MARKER', marker),
                    patch.object(guest.sys, 'argv', [guest.SCRIPT]),
                    patch.object(guest.sys, 'platform', 'linux'),
                    patch.object(guest.platform, 'machine', return_value='x86_64'),
                    patch.object(guest.os, 'getuid', return_value=0),
                    patch.object(guest.os, 'geteuid', return_value=0),
                    patch.object(guest.signal, 'signal'),
                    patch.object(guest.os, 'stat', side_effect=AssertionError('metadata after bad marker')) as metadata,
                    patch.object(guest, 'root_stages') as stages,
                    patch.object(guest, 'checked') as checked,
                    patch.object(guest, 'command') as command,
                    patch.object(guest, 'write_new') as write,
                    patch.object(Path, 'read_text') as read,
                    patch.object(Path, 'mkdir') as mkdir,
                    patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as output,
                ):
                    self.assertEqual(guest.seed_entrypoint(CONFIG), 1)
                for mocked in (metadata, stages, checked, command, write, read, mkdir):
                    mocked.assert_not_called()
                line = output.getvalue().strip()
                self.assertTrue(line.startswith(guest.ABORT_PREFIX))
                self.assertEqual(json.loads(line[len(guest.ABORT_PREFIX):]),
                                 {'schemaVersion': 1, 'token': TOKEN, 'error': 'unknown'})
                self.assertNotIn(str(root), line)

    def test_guard_passed_keeps_original_receipt_and_poweroff_behavior(self):
        for result in ({'complete': True}, guest.GuestFailure('later-stage-failed')):
            with patch.object(guest.sys, 'argv', [guest.SCRIPT]), patch.object(guest.signal, 'signal'), \
                 patch.object(guest, 'guard'), patch.object(guest, 'root_stages', side_effect=[result]), \
                 patch.object(guest, 'checked') as checked, \
                 patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as output:
                self.assertEqual(guest.seed_entrypoint(CONFIG), 0 if isinstance(result, dict) else 1)
            self.assertEqual(checked.call_args.args[0], ['/usr/bin/systemctl', 'poweroff', '--no-block'])
            self.assertNotIn('ENVY_RUNTIME_GUEST_ABORT ', output.getvalue())

    def test_rendered_main_exits_nonzero_with_only_safe_abort_on_guard_failure(self):
        program = guest.guest_program(TOKEN, network_metadata())
        body, entry = program.rsplit("if __name__ == '__main__':", 1)
        namespace = {'__name__': 'inert_seed'}
        exec(compile(body, '<rendered seed>', 'exec'), namespace)
        failure = namespace['GuestFailure']('file-changed')
        with patch.dict(namespace, {'main': Mock(side_effect=failure)}), \
             patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as output, \
             patch.object(guest.os, 'open', side_effect=AssertionError('host mutation')), \
             patch.object(guest.subprocess, 'Popen', side_effect=AssertionError('host spawn')):
            with self.assertRaises(SystemExit) as caught:
                exec(compile("if True:" + entry, '<rendered entry>', 'exec'), namespace)
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(json.loads(output.getvalue().split(' ', 1)[1]),
                         {'schemaVersion': 1, 'token': TOKEN, 'error': 'file-changed'})

    def test_bare_main_is_inert_before_all_io(self):
        source = Path(guest.__file__).read_text()
        with patch.object(guest.os, 'open', side_effect=AssertionError('host IO')), \
             patch.object(guest.subprocess, 'Popen', side_effect=AssertionError('host spawn')):
            with self.assertRaisesRegex(SystemExit, 'direct execution is forbidden'):
                exec(compile(source, '<bare host>', 'exec'), {'__name__': '__main__'})


class DialogueTests(Isolated):
    def dialogue(self, chunks):
        dialogue, replies = guest.SetupDialogue(), []
        for chunk in chunks:
            reply = dialogue.feed(chunk)
            if reply:
                replies.append(reply)
        return dialogue, replies

    def screens(self):
        return [b'Using existing checkout: /home/envy-runtime/.envy\r\nNix is already installed.\r\n',
                '\x1b[2JenvY Setup — menuconfig\r\nSystem username: envy-runtime\r\ns: save & exit'.encode(),
                b'? Apply changes and save? [y/n]: ',
                b'Commit hosts/linux/runtime-min.nix, secrets/secrets.yaml to git? [y/n]: ',
                b'All files saved successfully!\r\n? Apply configuration now (envy apply)? [y/n]: ']

    def test_semantic_multiscreen_save_and_two_explicit_declines(self):
        dialogue, replies = self.dialogue(self.screens())
        self.assertEqual(replies, [b's', b'y\n', b'n\n', b'n\n'])
        result = dialogue.finish(0)
        self.assertTrue(all(value is True for key, value in result.items() if key != 'exitCode'))
        self.assertEqual(result['exitCode'], 0)

    def test_bytewise_utf8_and_split_csi_and_cpr_requests(self):
        # Real PTYs can split escape sequences, UTF-8 and prompts at any byte.
        dialogue = guest.SetupDialogue()
        replies = []
        for screen in [b'\x1b[6n', *self.screens()]:
            for byte in screen:
                reply = dialogue.feed(bytes([byte]))
                if reply:
                    replies.append(reply)
        self.assertEqual(replies, [b'\x1b[1;1R', b's', b'y\n', b'n\n', b'n\n'])
        dialogue.finish(0)

    def test_unknown_prompt_missing_save_exit_failure_and_stage_order_fail_closed(self):
        for prompt in (b'? Import key? [y/n]: ', b'password:', b'Press Enter to install', b'Choose 1, 2, or 3: '):
            dialogue = guest.SetupDialogue()
            with self.subTest(prompt=prompt), self.assertRaises(guest.GuestFailure):
                dialogue.feed(prompt)
        with self.assertRaises(guest.GuestFailure):
            guest.SetupDialogue().feed(self.screens()[3])
        dialogue, _ = self.dialogue(self.screens()[:-1])
        with self.assertRaises(guest.GuestFailure):
            dialogue.feed(b'? Apply configuration now (envy apply)? [y/n]: ')
        for code in (0, 1, 97, -15):
            with self.assertRaises(guest.GuestFailure):
                guest.SetupDialogue().finish(code)
        dialogue, _ = self.dialogue(self.screens())
        with self.assertRaises(guest.GuestFailure):
            dialogue.finish(1)

    def test_answered_commit_redraw_and_output_limit_stop(self):
        dialogue, _ = self.dialogue(self.screens())
        self.assertEqual(dialogue.feed(self.screens()[3]), b'')
        with patch.object(guest, 'MAX_OUTPUT', 10), self.assertRaises(guest.GuestFailure):
            guest.SetupDialogue().feed(b'x' * 11)

    def fullscreen_screens(self):
        menu = ('envY Setup — menuconfig\r\n中文字段\r\n'
                'System password: [S]\r\nRestic repository password: [S]\r\n'
                'sudo password: [set]\r\ns: save & exit').encode()
        return [self.screens()[0], b'\x1b[?1049h\x1b[2J' + menu,
                b'\x1b[H' + menu + b'\x1b[?1049l', *self.screens()[2:]]

    def test_fullscreen_password_initial_and_done_redraw_at_every_split(self):
        screens = self.fullscreen_screens()
        for index, raw in enumerate(screens):
            for split in range(len(raw) + 1):
                with self.subTest(index=index, split=split):
                    chunks = screens[:index] + [raw[:split], raw[split:]] + screens[index + 1:]
                    dialogue, replies = self.dialogue(chunks)
                    self.assertEqual(b''.join(replies), b'sy\nn\nn\n')
                    dialogue.finish(0)

    def test_fullscreen_bytewise_utf8_cpr_and_repeated_questions(self):
        chunks = self.fullscreen_screens()
        rounds = [b'\x1b[6n', *chunks[:3]]
        for question in chunks[3:]:
            rounds.append(question + b'\x1b[6n\x1b[0m' + question)
        raw = b''.join(rounds)
        for bytewise in (False, True):
            dialogue, replies = self.dialogue([bytes([byte]) for byte in raw] if bytewise else rounds)
            self.assertEqual(b''.join(replies), b'\x1b[1;1Rsy\n\x1b[1;1Rn\n\x1b[1;1Rn\n\x1b[1;1R')
            dialogue.finish(0)
            self.assertEqual(dialogue.output_bytes, len(raw))
            self.assertEqual(dialogue.output_digest.hexdigest(), guest.sha(raw))

    def test_unknown_after_split_alternate_exit_is_never_masked(self):
        for unknown in (b'password:', b'Choose account:', b'Press Enter', b'? New question? [y/n]'):
            for split in range(len(b'\x1b[?1049l') + 1):
                dialogue, _ = self.dialogue(self.fullscreen_screens()[:2])
                wire = b'\x1b[?1049l'
                self.assertEqual(dialogue.feed(wire[:split]), b'')
                with self.subTest(unknown=unknown, split=split), \
                     self.assertRaisesRegex(guest.GuestFailure, 'unknown-pty-prompt'):
                    dialogue.feed(wire[split:] + unknown)
                self.assertEqual(dialogue.phase, 1)

    def test_past_known_plus_different_unknown_same_chunk_and_bytewise_refused(self):
        for past in range(2, 5):
            for unknown in (b'password:', b'Choose account:', b'Press Enter', b'? Other? [y/n]'):
                for bytewise in (False, True):
                    dialogue, _ = self.dialogue(self.screens()[:past + 1])
                    raw = self.screens()[past] + b'\r\n' + unknown
                    replies = []
                    with self.subTest(past=past, unknown=unknown, bytewise=bytewise), \
                         self.assertRaisesRegex(guest.GuestFailure, 'unknown-pty-prompt'):
                        for chunk in ([bytes([byte]) for byte in raw] if bytewise else [raw]):
                            replies.append(dialogue.feed(chunk))
                    self.assertEqual(b''.join(replies), b'')

    def test_future_questions_and_changed_commit_redraw_refused(self):
        for phase in range(3):
            for future in range(phase + 1, 4):
                for bytewise in (False, True):
                    dialogue, _ = self.dialogue(self.screens()[:phase + 1])
                    with self.subTest(phase=phase, future=future, bytewise=bytewise), \
                         self.assertRaisesRegex(guest.GuestFailure, 'pty-stage-order'):
                        raw = self.screens()[future + 1]
                        for chunk in ([bytes([byte]) for byte in raw] if bytewise else [raw]):
                            dialogue.feed(chunk)
        dialogue, _ = self.dialogue(self.screens())
        with self.assertRaisesRegex(guest.GuestFailure, 'unknown-pty-prompt'):
            dialogue.feed(b'Commit different-file to git? [y/n]')

    def test_known_question_substring_in_different_question_is_not_authorized(self):
        for question in (b'? Never Apply changes and save? [y/n]',
                         b'? Why Apply changes and save? [y/n]',
                         b'? Other? Apply changes and save? [y/n]'):
            for bytewise in (False, True):
                dialogue, _ = self.dialogue(self.screens()[:2])
                with self.subTest(question=question, bytewise=bytewise), \
                     self.assertRaisesRegex(guest.GuestFailure, 'unknown-pty-prompt'):
                    for chunk in ([bytes([byte]) for byte in question] if bytewise else [question]):
                        dialogue.feed(chunk)

    def test_osc_bel_and_st_before_current_question_at_every_split(self):
        for terminator in (b'\x07', b'\x1b\\'):
            title = b'\x1b]0;title' + terminator
            for index, expected in ((2, b'y\n'), (3, b'n\n'), (4, b'n\n')):
                raw = title + b'\r\n' + self.screens()[index]
                variants = [[raw[:split], raw[split:]] for split in range(len(raw) + 1)]
                variants.append([bytes([byte]) for byte in raw])
                for chunks in variants:
                    with self.subTest(terminator=terminator, index=index, chunks=len(chunks)):
                        dialogue, _ = self.dialogue(self.screens()[:index])
                        replies = [dialogue.feed(chunk) for chunk in chunks]
                        self.assertEqual(b''.join(replies), expected)
                        for screen in self.screens()[index + 1:]:
                            dialogue.feed(screen)
                        dialogue.finish(0)

    def test_phase_four_osc_title_cannot_hide_unknown_question(self):
        for terminator in (b'\x07', b'\x1b\\'):
            raw = b'\x1b]0;title' + terminator + b'\r\n? Unexpected question? [y/n]'
            variants = [[raw], [bytes([byte]) for byte in raw]]
            variants.extend([raw[:split], raw[split:]] for split in range(len(raw) + 1))
            for chunks in variants:
                dialogue, _ = self.dialogue(self.screens())
                with self.subTest(terminator=terminator, chunks=len(chunks)), \
                     self.assertRaisesRegex(guest.GuestFailure, 'unknown-pty-prompt'):
                    for chunk in chunks:
                        dialogue.feed(chunk)

    def test_malformed_csi_with_unknown_tail_cannot_finish_successfully(self):
        raw = b'\x1b[\r\n? Unexpected? [y/n]'
        for chunks in ([raw], [bytes([byte]) for byte in raw]):
            dialogue, _ = self.dialogue(self.screens())
            with self.subTest(chunks=len(chunks)), \
                 self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
                for chunk in chunks:
                    dialogue.feed(chunk)
                dialogue.finish(0)

    def test_incomplete_control_at_eof_cannot_finish_successfully(self):
        for raw in (b'\x1b', b'\x1b[', b'\x1b[?1049', b'\x1b]0;title', b'\x1b]0;title\x1b'):
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                dialogue, _ = self.dialogue(self.screens())
                for chunk in chunks:
                    self.assertEqual(dialogue.feed(chunk), b'')
                with self.subTest(raw=raw, chunks=len(chunks)), \
                     self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
                    dialogue.finish(0)

    def test_incomplete_utf8_at_eof_cannot_finish_successfully(self):
        for raw in (b'\xe4', b'\xe4\xb8', b'\xf0\x9f\x98'):
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                dialogue, _ = self.dialogue(self.screens())
                for chunk in chunks:
                    self.assertEqual(dialogue.feed(chunk), b'')
                with self.subTest(raw=raw, chunks=len(chunks)), \
                     self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
                    dialogue.finish(0)

    def test_complete_controls_and_nonempty_plain_tail_remain_valid(self):
        for title in (b'\x1b]0;title\x07', b'\x1b]0;title\x1b\\'):
            raw = title + '\r\nDone 中文\r\n'.encode() + b'\x1b[0m'
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                dialogue, _ = self.dialogue(self.screens())
                for chunk in chunks:
                    self.assertEqual(dialogue.feed(chunk), b'')
                self.assertTrue(dialogue.text)
                self.assertEqual(dialogue.wire, '')
                self.assertEqual(dialogue.decoder.getstate()[0], b'')
                self.assertEqual(dialogue.finish(0)['exitCode'], 0)

    def test_alternate_reentry_after_menu_cannot_hide_unknown_tail(self):
        raw = b'\x1b[?1049h\r\n? Unexpected? [y/n]\x1b[?1049l'
        for phase in range(1, 5):
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                dialogue, _ = self.dialogue(self.fullscreen_screens()[:phase + 2])
                with self.subTest(phase=phase, chunks=len(chunks)), \
                     self.assertRaisesRegex(guest.GuestFailure, 'pty-stage-order'):
                    for chunk in chunks:
                        dialogue.feed(chunk)

    def test_unclosed_or_unexpected_alternate_frame_cannot_finish(self):
        for raw in (b'\x1b[?1049h', b'\x1b[?1049l'):
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                dialogue, _ = self.dialogue(self.screens())
                with self.subTest(raw=raw, chunks=len(chunks)), \
                     self.assertRaises(guest.GuestFailure):
                    for chunk in chunks:
                        dialogue.feed(chunk)
                    dialogue.finish(0)
        # A pending original menu frame is independently forbidden at EOF.
        dialogue, _ = self.dialogue(self.fullscreen_screens()[:2])
        self.assertTrue(dialogue.alternate)
        with self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
            dialogue.finish(0)
        # Isolate the EOF frame guard from the phase/progress guards.
        dialogue, _ = self.dialogue(self.screens())
        dialogue.alternate = True
        with self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
            dialogue.finish(0)
        dialogue.alternate = False
        self.assertEqual(dialogue.finish(0)['exitCode'], 0)

    def test_feed_batch_cannot_authorize_future_questions_after_current_reply(self):
        for separator in (b'\r\n', b'\r\n\x1b[0m'):
            dialogue, _ = self.dialogue(self.screens()[:2])
            raw = separator.join(self.screens()[2:])
            with self.subTest(separator=separator), \
                 self.assertRaisesRegex(guest.GuestFailure, 'pty-stage-order'):
                dialogue.feed(raw)
        dialogue, _ = self.dialogue(self.screens()[:3])
        self.assertEqual(dialogue.feed(self.screens()[2] + b'\r\n' + self.screens()[3]), b'n\n')
        self.assertEqual(dialogue.phase, 3)

    def test_consumed_questions_do_not_reset_total_output_bound(self):
        dialogue, _ = self.dialogue(self.screens())
        used = dialogue.output_bytes
        with patch.object(guest, 'MAX_OUTPUT', used + len(self.screens()[2])):
            self.assertEqual(dialogue.feed(self.screens()[2]), b'')
            with self.assertRaisesRegex(guest.GuestFailure, 'pty-output-limit'):
                dialogue.feed(self.screens()[2])



class SetupObservationTests(Isolated):
    screens = DialogueTests.screens
    def setUp(self):
        super().setUp()
        self.env = guest.environment()
        self.lock = self.enterContext(patch.object(guest, 'verify_lock'))
        self.git = self.enterContext(patch.object(guest, 'git', side_effect=[b'rev', b'1']))
        self.read = self.enterContext(patch.object(guest, 'private_read', return_value=
            b'home.file.".config/envy-runtime/proof".text'))
        self.apply = self.enterContext(patch.object(guest, 'apply_stage'))
        self.tui = self.enterContext(patch.object(guest, 'tui_tests'))

    def run_setup(self, chunks=(), error=None, code=0):
        def command(*args, **kwargs):
            self.dialogue_instance = kwargs['dialogue']
            for chunk in chunks:
                self.dialogue_instance.feed(chunk)
            if error is not None:
                raise error
            return code, b'UNTRUSTED RETURN BODY'
        with patch.object(guest, 'command', side_effect=command) as called:
            stages, failure = guest.lifecycle(self.env, b'rev')
        self.assertEqual(called.call_count, 1)
        self.assertEqual(failure, 'cancelled' if isinstance(error, KeyboardInterrupt) else 'failed-or-unknown:setup')
        self.assertEqual(set(stages), {'setup'})
        self.apply.assert_not_called()
        self.tui.assert_not_called()
        self.serial.assert_not_called()
        return stages['setup']

    def assert_closed(self, row, *, code, kind, raw=b'', phase=0, saved=False, existing=False, checkout=False,
                      error_number=None):
        self.assertEqual(row, {
            'status': 'partial-or-unknown', 'errorCode': code, 'exceptionKind': kind,
            'errno': error_number, 'dialoguePhase': phase, 'saved': saved,
            'existingNix': existing, 'checkout': checkout,
            'outputBytes': len(raw), 'outputSha256': guest.sha(raw),
            'childFailure': {'kind': 'unknown', 'exceptionClass': None, 'lastKnownFrame': None,
                             'invalidFieldIds': []},
            'initialManifestEvaluation': {'status': 'unknown', 'exitCode': None, 'debugEnabled': True}})
        self.assertIs(type(row['dialoguePhase']), int)
        self.assertIs(type(row['outputBytes']), int)
        for key in ('saved', 'existingNix', 'checkout'):
            self.assertIs(type(row[key]), bool)
        self.assertNotIn('SENSITIVE', json.dumps(row))
        self.assertNotIn('UNTRUSTED', json.dumps(row))
        self.assertFalse({'commitDeclined', 'applyDeclined', 'interactive', 'exitCode'} & set(row))

    def test_unknown_prompt_and_out_of_order_screen_are_closed_without_answers(self):
        for raw, code in ((b'SENSITIVE prompt [y/n]: ', 'unknown-pty-prompt'),
                          (self.screens()[3], 'pty-stage-order')):
            row = self.run_setup([raw])
            self.assert_closed(row, code=code, kind='GuestFailure', raw=raw)
            self.assertEqual(self.dialogue_instance.phase, 0)

    def test_finish_incomplete_at_every_phase_and_nonzero_exit_never_claims_declines(self):
        for phase in range(5):
            chunks = self.screens()[:phase + 1]
            row = self.run_setup(chunks, code=1 if phase == 4 else 0)
            self.assert_closed(row, code='setup-incomplete', kind='GuestFailure', raw=b''.join(chunks),
                               phase=phase, saved=phase == 4, existing=True, checkout=True)

    def test_saved_is_observed_and_missing_saved_marker_is_not_invented(self):
        chunks = [*self.screens()[:-1], b'Apply configuration now (envy apply)? [y/n]']
        row = self.run_setup(chunks)
        self.assert_closed(row, code='setup-not-saved', kind='GuestFailure', raw=b''.join(chunks),
                           phase=3, existing=True, checkout=True)

    def test_fullscreen_exit_unknown_remains_partial_and_not_a_formal_receipt(self):
        chunks = DialogueTests.fullscreen_screens(self)[:2] + [b'\x1b[?1049', b'lpassword:']
        row = self.run_setup(chunks)
        self.assert_closed(row, code='unknown-pty-prompt', kind='GuestFailure', raw=b''.join(chunks),
                           phase=1, existing=True, checkout=True)
        fixture = receipt_fixtures.ReceiptTests()
        fixture.setUp()
        receipt = copy.deepcopy(fixture.row)
        receipt['stages']['setup'] = row
        for transport in guest.TRANSPORTS:
            if transport == 'guest-fetch':
                receipt.update(transport=transport, evidence=acceptance.NETWORK_EVIDENCE)
            with self.assertRaisesRegex(acceptance.vm.ProbeError, 'invalid-acceptance-receipt'):
                acceptance.receipt_from(fixture.raw(receipt), TOKEN, fixture.source, transport=transport)

    def test_eof_residue_has_closed_partial_diagnostic_after_phase_four(self):
        for tail in (b'\x1b]0;SENSITIVE', b'\x1b[\r\n? SENSITIVE? [y/n]', b'\xe4\xb8'):
            # run_setup checks all later execution boundaries remain uncalled.
            chunks = self.screens() + [tail]
            row = self.run_setup(chunks)
            self.assert_closed(row, code='setup-incomplete', kind='GuestFailure', raw=b''.join(chunks),
                               phase=4, saved=True, existing=True, checkout=True)

    def test_all_exception_families_are_fixed_and_dynamic_names_args_paths_and_bodies_never_leave(self):
        class SENSITIVE_DYNAMIC_NAME(ValueError):
            def __str__(self):
                raise AssertionError('exception rendering forbidden')
        errors = [(OSError(errno.EIO, 'SENSITIVE body', '/SENSITIVE/path'), 'OSError', errno.EIO),
                  (ValueError('SENSITIVE value'), 'ValueError', None),
                  (KeyError('SENSITIVE key'), 'KeyError', None),
                  (TypeError('SENSITIVE input'), 'TypeError', None),
                  (guest.subprocess.CalledProcessError(97, ['SENSITIVE command'],
                       output=b'SENSITIVE output', stderr=b'SENSITIVE stderr'), 'SubprocessError', None),
                  (guest.subprocess.TimeoutExpired(['SENSITIVE command'], 1, output=b'SENSITIVE output'),
                       'SubprocessError', None),
                  (KeyboardInterrupt('SENSITIVE cancel'), 'KeyboardInterrupt', None),
                  (SENSITIVE_DYNAMIC_NAME('SENSITIVE args'), 'ValueError', None)]
        raw = b'SENSITIVE PTY transcript'
        for error, kind, error_number in errors:
            with self.subTest(kind=kind):
                row = self.run_setup([raw], error=error)
                self.assert_closed(row, code='cancelled' if kind == 'KeyboardInterrupt' else 'unknown',
                                   kind=kind, error_number=error_number, raw=raw)

    def test_only_fixed_guest_codes_are_reported_without_serializing_nonstring_args(self):
        for args, code in ((('command-timeout',), 'command-timeout'), (('timeout',), 'timeout'),
                           (('command-output-limit',), 'command-output-limit'),
                           (('SENSITIVE prompt',), 'unknown'), ((False,), 'unknown'),
                           (({'SENSITIVE key': 'SENSITIVE value'},), 'unknown'),
                           (('timeout', 'SENSITIVE extra'), 'unknown'), ((), 'unknown')):
            with self.subTest(code=code):
                self.assert_closed(self.run_setup(error=guest.GuestFailure(*args)), code=code, kind='GuestFailure')

    def test_errno_strict_integer_bounds_and_non_os_errno_are_filtered(self):
        for value in (None, False, True, '5', -1, 0, 1, 4095, 4096, 2**128):
            error = OSError('SENSITIVE error')
            error.errno = value
            row = self.run_setup(error=error)
            self.assert_closed(row, code='unknown', kind='OSError',
                               error_number=value if type(value) is int and 1 <= value <= 4095 else None)
        error = ValueError('SENSITIVE error')
        error.errno = errno.EIO
        self.assert_closed(self.run_setup(error=error), code='unknown', kind='ValueError')

    def test_output_digest_is_raw_across_split_utf8_csi_cpr_and_cleared_screens(self):
        raw = b'\x1b[6n' + b''.join(self.screens()) + b'SENSITIVE \xff'
        chunks = [bytes([byte]) for byte in raw]
        self.assert_closed(self.run_setup(chunks, error=ValueError('SENSITIVE')),
                           code='unknown', kind='ValueError', raw=raw, phase=4,
                           saved=True, existing=True, checkout=True)

    def test_feed_output_limit_counts_the_rejected_bytes_without_body(self):
        raw = b'SENSITIVE' * 5
        with patch.object(guest, 'MAX_OUTPUT', 10):
            row = self.run_setup([raw])
        self.assert_closed(row, code='pty-output-limit', kind='GuestFailure', raw=raw)

    def test_lock_drift_after_return_is_partial_and_after_failure_preserves_first_error(self):
        self.lock.side_effect = guest.GuestFailure('lock-drift')
        self.assert_closed(self.run_setup(), code='lock-drift', kind='GuestFailure')
        for error, code, kind in ((ValueError('SENSITIVE'), 'unknown', 'ValueError'),
                                  (guest.GuestFailure('command-timeout'), 'command-timeout', 'GuestFailure'),
                                  (KeyboardInterrupt('SENSITIVE'), 'cancelled', 'KeyboardInterrupt')):
            row = self.run_setup(error=error)
            self.assert_closed(row, code=code, kind=kind)
        self.assertEqual(self.lock.call_count, 4)

    def test_post_dialogue_commit_and_proof_checks_remain_required(self):
        for failed in ('commit', 'proof'):
            self.git.side_effect = [b'wrong'] if failed == 'commit' else [b'rev', b'1']
            self.read.return_value = b'SENSITIVE missing proof'
            chunks = self.screens()
            row = self.run_setup(chunks)
            self.assert_closed(row, code='setup-unexpected-commit' if failed == 'commit' else 'setup-lost-proof',
                               kind='GuestFailure', raw=b''.join(chunks), phase=4,
                               saved=True, existing=True, checkout=True)

    def test_complete_setup_success_keeps_original_receipt_shape(self):
        def command(*args, **kwargs):
            for chunk in self.screens():
                kwargs['dialogue'].feed(chunk)
            return 0, b'SENSITIVE output'
        with patch.object(guest, 'command', side_effect=command):
            result = guest.setup_stage(self.env, b'rev')
        self.assertEqual(result, {'exitCode': 0, 'interactive': True, 'saved': True,
                                 'commitDeclined': True, 'applyDeclined': True, 'existingNixBranch': True})
        self.lock.assert_called_once_with(self.env)

    def test_partial_observation_serializes_without_secrets_and_host_still_refuses_in_both_modes(self):
        fixture = receipt_fixtures.ReceiptTests()
        fixture.setUp()
        sentinel = b'SENSITIVE PTY body'
        row = self.run_setup([sentinel], error=ValueError('SENSITIVE dynamic error'))
        self.assertIn('SENSITIVE', sentinel.decode())
        for transport in guest.TRANSPORTS:
            receipt = copy.deepcopy(fixture.row)
            receipt.update(stages={'setup': row}, error='failed-or-unknown:setup')
            if transport == 'guest-fetch':
                receipt.update(transport=transport, evidence=acceptance.NETWORK_EVIDENCE)
            serial_raw = bytearray()
            def write(fd, data):
                self.assertEqual(fd, 71)
                serial_raw.extend(data)
                return len(data)
            with patch.object(guest.os, 'getuid', return_value=0), \
                 patch.object(guest.os, 'geteuid', return_value=0), \
                 patch.object(guest.os, 'open', return_value=71) as opened, \
                 patch.object(guest.os, 'fstat', return_value=SimpleNamespace(
                     st_mode=stat.S_IFCHR | 0o600, st_rdev=os.makedev(4, 64))), \
                 patch.object(guest.os, 'write', side_effect=write), patch.object(guest.os, 'close'):
                EMIT_SERIAL(guest.INSTALL_PREFIX, fixture.installed)
                EMIT_SERIAL(guest.PREFIX, receipt)
            self.assertTrue(all(call.args[0] == '/dev/ttyS0' for call in opened.call_args_list))
            raw = bytes(serial_raw)
            self.assertNotIn(b'SENSITIVE', raw)
            self.assertIn(guest.sha(sentinel).encode(), raw)
            with self.assertRaisesRegex(acceptance.vm.ProbeError, 'mismatched-acceptance-receipt'):
                acceptance.receipt_from(raw, TOKEN, fixture.source, transport=transport)
            receipt['error'] = None
            receipt['stages'] = {**fixture.row['stages'], 'setup': row}
            with self.assertRaisesRegex(acceptance.vm.ProbeError, 'invalid-acceptance-receipt'):
                acceptance.receipt_from(fixture.raw(receipt), TOKEN, fixture.source, transport=transport)

    def test_real_pty_failure_timeout_cancel_and_init_error_have_safe_observations_and_owned_cleanup(self):
        # All descriptors, reads, process IDs and executable boundaries are fake.
        for reason in ('feed', 'read', 'timeout', 'cancel', 'init'):
            with self.subTest(reason=reason), ExitStack() as stack:
                def stub(target, name, **kwargs):
                    return stack.enter_context(patch.object(target, name, **kwargs))
                fork = stub(guest.pty, 'fork', return_value=(424242, 99),
                            side_effect=OSError(errno.EAGAIN, 'SENSITIVE fork') if reason == 'init' else None)
                stub(guest.select, 'select', return_value=([99], [], []))
                stub(guest.os, 'read', return_value=b'SENSITIVE prompt [y/n]: ',
                     side_effect=ValueError('SENSITIVE read') if reason == 'read' else None)
                stub(guest.os, 'write', side_effect=AssertionError('no speculative answers'))
                waited = stub(guest.os, 'waitpid', return_value=(424242, 0))
                closed = stub(guest.os, 'close')
                stopped = stub(guest, 'stop')
                stub(guest, 'tick', side_effect=[None, KeyboardInterrupt('SENSITIVE')] if reason == 'cancel' else None)
                stub(guest.time, 'monotonic', side_effect=[1, 2000] if reason == 'timeout' else None, return_value=1)
                stages, error = guest.lifecycle(self.env, b'rev')
                row = stages['setup']
                self.assertEqual(error, 'cancelled' if reason == 'cancel' else 'failed-or-unknown:setup')
                self.assertEqual(row['status'], 'partial-or-unknown')
                self.assertEqual(row['errorCode'], {'feed': 'unknown-pty-prompt', 'timeout': 'command-timeout',
                                                  'cancel': 'cancelled'}.get(reason, 'unknown'))
                self.assertNotIn('SENSITIVE', json.dumps(row))
                self.assertEqual(guest.COMMAND_FACTS, [])
                self.assertEqual(fork.call_count, 1)
                if reason == 'init':
                    stopped.assert_not_called()
                    waited.assert_not_called()
                    closed.assert_not_called()
                    self.assertEqual(row['errno'], errno.EAGAIN)
                else:
                    stopped.assert_called_once_with(424242)
                    waited.assert_called_once_with(424242, 0)
                    closed.assert_called_once_with(99)
                self.assertEqual(row['outputBytes'], len(b'SENSITIVE prompt [y/n]: ') if reason == 'feed' else 0)
        self.apply.assert_not_called()
        self.tui.assert_not_called()
        self.serial.assert_not_called()

    def test_pty_cleanup_errors_do_not_mask_feed_first_error_or_skip_reap_and_close(self):
        for boundary in ('stop', 'wait'):
            with patch.object(guest.pty, 'fork', return_value=(424242, 99)), \
                 patch.object(guest.select, 'select', return_value=([99], [], [])), \
                 patch.object(guest.os, 'read', return_value=b'SENSITIVE [y/n]'), \
                 patch.object(guest, 'stop', side_effect=OSError(errno.EPERM, 'SENSITIVE cleanup')
                              if boundary == 'stop' else None) as stop, \
                 patch.object(guest.os, 'waitpid', side_effect=OSError(errno.ECHILD, 'SENSITIVE reap')
                              if boundary == 'wait' else None) as waited, \
                 patch.object(guest.os, 'close') as closed:
                stages, error = guest.lifecycle(self.env, b'rev')
            self.assertEqual(error, 'failed-or-unknown:setup')
            self.assertEqual(stages['setup']['errorCode'], 'unknown-pty-prompt')
            stop.assert_called_once_with(424242)
            waited.assert_called_once_with(424242, 0)
            closed.assert_called_once_with(99)


class ChildFailureTests(Isolated):
    screens = DialogueTests.screens

    def observation(self, chunks):
        dialogue = guest.SetupDialogue()
        for chunk in chunks:
            self.assertEqual(dialogue.feed(chunk), b'')
            self.assertLessEqual(len(dialogue.diagnostic_tail), 8192)
        row = dialogue.failure_observation(guest.GuestFailure('setup-incomplete'))
        self.assertEqual(row['status'], 'partial-or-unknown')
        self.assertEqual(row['errorCode'], 'setup-incomplete')
        self.assertFalse({'exitCode', 'interactive', 'commitDeclined', 'applyDeclined'} & set(row))
        self.assertEqual(row['outputBytes'], sum(map(len, chunks)))
        self.assertEqual(row['outputSha256'], guest.sha(b''.join(chunks)))
        for sentinel in ('SENSITIVE', '/fake/private', 'fake@example.invalid', 'FAKE_TOKEN', 'FAKE_ENV',
                         'FAKE_ARGV', 'Traceback (most recent call last):', 'File "'):
            self.assertNotIn(sentinel, json.dumps(row))
        self.serial.assert_not_called()
        return row['childFailure'], dialogue

    def assert_child(self, chunks, *, kind='unknown', exception=None, frame=None, invalid=()):
        child, dialogue = self.observation(chunks)
        self.assertEqual(child, {'kind': kind, 'exceptionClass': exception, 'lastKnownFrame': frame,
                                 'invalidFieldIds': list(invalid)})
        return dialogue

    def traceback(self, name=b'KeyError'):
        return (b'Traceback (most recent call last):\r\n'
                b'  File "/fake/private/SENSITIVE.py", line 123, in _run_setup\r\n'
                b'    SENSITIVE = FAKE_ENV + FAKE_ARGV\r\n'
                b'  File "/fake/private/FAKE_TOKEN.py", line 456, in show_changes\r\n'
                b'    source("fake@example.invalid")\r\n'
                + name + b': SENSITIVE FAKE_TOKEN /fake/private FAKE_ENV FAKE_ARGV fake@example.invalid\r\n')

    def test_traceback_all_splits_and_bytewise_strip_csi_and_osc_without_disclosure(self):
        for terminator in (b'\x07', b'\x1b\\'):
            raw = (b'\x1b]0;SENSITIVE FAKE_TOKEN' + terminator + b'\x1b[31m'
                   + self.traceback() + b'\x1b[0m')
            variants = [[raw], [bytes([byte]) for byte in raw]]
            variants.extend([raw[:split], raw[split:]] for split in range(len(raw) + 1))
            for chunks in variants:
                with self.subTest(terminator=terminator, chunks=len(chunks)):
                    self.assert_child(chunks, kind='python-exception', exception='KeyError', frame='show_changes')

    def test_terminal_class_without_message_is_recognized_full_and_bytewise(self):
        for name in guest.SETUP_CHILD_CLASSES:
            raw = self.traceback(name.encode()).split(name.encode() + b':', 1)[0] + name.encode() + b'\r\n'
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                self.assert_child(chunks, kind='python-exception', exception=name, frame='show_changes')

        for token, name in (('subprocess.CalledProcessError', 'CalledProcessError'),
                            ('subprocess.TimeoutExpired', 'TimeoutExpired')):
            raw = self.traceback(token.encode()).split(token.encode() + b':', 1)[0] + token.encode() + b'\n'
            self.assert_child([raw], kind='python-exception', exception=name, frame='show_changes')

    def test_terminal_exception_body_cannot_replace_real_frame(self):
        raw = (b'Traceback (most recent call last):\n'
               b'  File "/fake/private/SENSITIVE.py", line 1, in _edit\n'
               b'ValueError: SENSITIVE multiline body\n'
               b'  File "/fake/private/FAKE_TOKEN.py", line 2, in show_changes\n'
               b'KeyError: SENSITIVE injected class\n')
        for chunks in ([raw], [bytes([byte]) for byte in raw]):
            self.assert_child(chunks, kind='python-exception', exception='ValueError', frame='_edit')

    def test_unknown_terminal_exception_body_cannot_inject_known_class_or_frame(self):
        for terminal in (b'SENSITIVE_UNKNOWN: FAKE_TOKEN', b'SENSITIVE_UNKNOWN',
                         b'SENSITIVE_MODULE.Error: FAKE_TOKEN', b'SENSITIVE nonclass terminal'):
            raw = (b'Traceback (most recent call last):\n'
                   b'  File "/fake/private/SENSITIVE.py", line 1, in _run_setup\n'
                   + terminal + b'\n'
                   b'  File "/fake/private/FAKE_TOKEN.py", line 2, in show_changes\n'
                   b'ValueError: SENSITIVE injected class\n')
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                self.assert_child(chunks, frame='_run_setup')

    def test_python313_source_caret_and_later_chains_preserve_first_segment(self):
        first = (b'Traceback (most recent call last):\n'
                 b'  File "/fake/private/SENSITIVE.py", line 1, in _edit\n'
                 b'    raise ValueError(SENSITIVE)\n'
                 b'    ~~~~~~~~~~~~~~~~^^^^^^^^^^\n'
                 b'ValueError: SENSITIVE\n')
        second = (b'Traceback (most recent call last):\n'
                  b'  File "/fake/private/FAKE_TOKEN.py", line 2, in show_changes\n'
                  b'    call(SENSITIVE)\n'
                  b'    ~~~~^^^^^^^^^^^\n'
                  b'RuntimeError\n')
        for separator in (b'The above exception was the direct cause of the following exception:',
                          b'During handling of the above exception, another exception occurred:'):
            raw = first + b'\n' + separator + b'\n\n' + second
            variants = [[raw], [bytes([byte]) for byte in raw]]
            variants.extend([raw[:split], raw[split:]] for split in range(len(raw) + 1))
            for chunks in variants:
                self.assert_child(chunks, kind='python-exception', exception='ValueError', frame='_edit')

    def test_body_headers_and_exact_chain_separators_never_reopen_frames(self):
        first = (b'Traceback (most recent call last):\n'
                 b'  File "/fake/private/SENSITIVE.py", line 1, in _edit\n'
                 b'ValueError: SENSITIVE\n')
        for separator in (b'', b'\n', b'\nSENSITIVE chain separator\n\n',
                          b'\nThe above exception was the direct cause of the following exception:\n',
                          b'\nThe above exception was the direct cause of the following exception:\n\n',
                          b'\nDuring handling of the above exception, another exception occurred:\n\n'):
            raw = first + separator + self.traceback()
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                self.assert_child(chunks, kind='python-exception', exception='ValueError', frame='_edit')

    def test_every_reviewed_exception_and_frame_is_returned_only_as_static_name(self):
        for name in guest.SETUP_CHILD_CLASSES:
            self.assert_child([self.traceback(name.encode())], kind='python-exception',
                              exception=name, frame='show_changes')
        for name in ('CalledProcessError', 'TimeoutExpired'):
            self.assert_child([self.traceback(('subprocess.' + name).encode())],
                              kind='python-exception', exception=name, frame='show_changes')
        for frame in guest.SETUP_CHILD_FRAMES:
            raw = (b'Traceback (most recent call last):\n'
                   b'  File "/fake/private/SENSITIVE.py", line 1, in ' + frame.encode()
                   + b'\nNameError: SENSITIVE\n')
            self.assert_child([raw], kind='python-exception', exception='NameError', frame=frame)

    def test_unknown_classes_frames_headerless_and_quoted_source_do_not_guess(self):
        self.assert_child([self.traceback(b'SENSITIVE_DYNAMIC_CLASS')], frame='show_changes')
        raw = (b'Traceback (most recent call last):\n'
               b'  File "/fake/private/SENSITIVE.py", line 1, in SENSITIVE_DYNAMIC_FUNCTION\n'
               b'TypeError: SENSITIVE\n')
        self.assert_child([raw], kind='python-exception', exception='TypeError')
        for raw in (b'KeyError: SENSITIVE\n', self.traceback().split(b'\r\n', 1)[1],
                    b'    print("Traceback (most recent call last):")\nKeyError: SENSITIVE\n',
                    b' Traceback (most recent call last):\nKeyError: SENSITIVE\n',
                    b'Traceback (most recent call last):\n    KeyError: SENSITIVE\n',
                    b'Traceback (most recent call last):\nSENSITIVE.CalledProcessError: SENSITIVE\n'):
            self.assert_child([raw])

    def test_last_known_frame_and_first_traceback_survive_later_headers(self):
        raw = (b'Traceback (most recent call last):\n'
               b'  File "/fake/private/SENSITIVE.py", line 1, in _edit\n'
               b'  File "/fake/private/FAKE_TOKEN.py", line 2, in SENSITIVE_DYNAMIC_FUNCTION\n'
               b'ValueError: SENSITIVE\n')
        self.assert_child([raw], kind='python-exception', exception='ValueError', frame='_edit')
        self.assert_child([raw, b'\nDuring handling of the above exception, another exception occurred:\n\n'
                           b'Traceback (most recent call last):\nSENSITIVE_DYNAMIC_CLASS: FAKE_TOKEN\n'],
                          kind='python-exception', exception='ValueError', frame='_edit')

    def test_fixed_validation_messages_and_schema_ids_deduplicate_and_remain_bounded(self):
        for field_id in guest.SETUP_FIELD_IDS:
            raw = ('ERR setup: required field is empty field=' + field_id + '\r\n').encode()
            self.assert_child([raw, raw], kind='validation-rejected', invalid=(field_id,))
        for message in guest.SETUP_VALIDATION_MESSAGES:
            raw = ('ERR setup: ' + message + ' field=envy.git.email\n').encode()
            self.assert_child([raw], kind='validation-rejected', invalid=('envy.git.email',))
        raw = b''.join(('ERR setup: required field is empty field=' + field_id + '\n').encode()
                       for field_id in reversed(guest.SETUP_FIELD_IDS))
        self.assert_child([raw, raw], kind='validation-rejected', invalid=guest.SETUP_FIELD_IDS)

    def test_validation_full_chunked_wrapped_and_split_control_streams(self):
        raw = (b'\x1b[31mERR\x1b[0m setup: Please enter a valid\r\nemail address\r\n'
               b'field=envy.git.email\r\n'
               b'ERR setup: required field is empty field=llm/steps/apikey\r\n'
               b'    SENSITIVE FAKE_TOKEN fake@example.invalid\r\n')
        variants = [[raw], [bytes([byte]) for byte in raw]]
        variants.extend([raw[:split], raw[split:]] for split in range(len(raw) + 1))
        for chunks in variants:
            self.assert_child(chunks, kind='validation-rejected', invalid=('envy.git.email', 'llm/steps/apikey'))

    def test_unknown_ids_values_and_quoted_traceback_log_source_are_not_validation(self):
        for raw in (b'ERR setup: required field is empty field=SENSITIVE_UNKNOWN_ID\n',
                    b'ERR setup: required field is empty field=envy.git.email.SENSITIVE\n',
                    b'ERR setup: SENSITIVE arbitrary message field=envy.git.email\n',
                    b'ERR setup: required field is empty field=envy.git.email value=SENSITIVE\n',
                    b'WARN setup: required field is empty field=envy.git.email\n',
                    b'    log.error("setup", "required field is empty", field="envy.git.email")\n',
                    b'    print("ERR setup: required field is empty field=envy.git.email")\n',
                    b'\x1b]0;ERR setup: required field is empty field=envy.git.email\x07'):
            self.assert_child([raw])
        # Even exact-looking text in a multiline exception body is not a log.
        self.assert_child([self.traceback(), b'ERR setup: required field is empty field=envy.git.email\n'],
                          kind='python-exception', exception='KeyError', frame='show_changes')
        raw = (b'Traceback (most recent call last):\n'
               b'  File "/fake/private/SENSITIVE.py", line 1, in _validate_field\n'
               b'    log.error("setup", "required field is empty", field="envy.git.email")\n'
               b'KeyError: SENSITIVE\n')
        self.assert_child([raw], kind='python-exception', exception='KeyError', frame='_validate_field')

    def test_tail_eight_kib_boundary_complete_header_and_multibyte_chunks(self):
        trace = self.traceback()
        for retained in (8191, 8192, 8193):
            padding = b'SENSITIVE' * 1100 + b'\n'
            raw = padding + trace + b'x' * (retained - len(trace))
            expected = {'kind': 'python-exception', 'exception': 'KeyError', 'frame': 'show_changes'} if retained <= 8192 else {}
            for chunks in ([raw], [bytes([byte]) for byte in raw]):
                self.assert_child(chunks, **expected)
        raw = ('中文' * 1600 + '\n').encode() + trace
        dialogue = self.assert_child([bytes([byte]) for byte in raw], kind='python-exception',
                                     exception='KeyError', frame='show_changes')
        self.assertEqual(len(dialogue.diagnostic_tail), 8192)
        # The retained suffix starts with a plausible header halfway through a line.
        raw = b'SENSITIVE' + trace + b'x' * (8192 - len(trace))
        for chunks in ([raw], [bytes([byte]) for byte in raw]):
            self.assert_child(chunks)
        raw = b'ERR setup: required field is empty field=envy.git.email\n' + b'x' * 8192
        self.assert_child([raw])

    def test_passive_tail_survives_menu_clear_and_does_not_change_answers_or_success(self):
        dialogue = guest.SetupDialogue()
        prefix = self.traceback()
        self.assertEqual(dialogue.feed(prefix), b'')
        replies = [dialogue.feed(chunk) for chunk in DialogueTests.fullscreen_screens(self)]
        self.assertEqual(b''.join(replies), b'sy\nn\nn\n')
        self.assertEqual(dialogue.finish(0), {'exitCode': 0, 'interactive': True, 'saved': True,
                                            'commitDeclined': True, 'applyDeclined': True,
                                            'existingNixBranch': True})
        child = dialogue.failure_observation(guest.GuestFailure('setup-incomplete'))['childFailure']
        self.assertEqual(child, {'kind': 'python-exception', 'exceptionClass': 'KeyError',
                                 'lastKnownFrame': 'show_changes', 'invalidFieldIds': []})

    def test_diagnostic_never_authorizes_unknown_or_future_prompt_or_partial_success(self):
        for raw, code in ((b'? SENSITIVE question? [y/n]', 'unknown-pty-prompt'),
                          (b'Apply changes and save? [y/n]', 'pty-stage-order')):
            dialogue = guest.SetupDialogue()
            self.assertEqual(dialogue.feed(self.traceback()), b'')
            with self.assertRaisesRegex(guest.GuestFailure, code) as caught:
                dialogue.feed(raw)
            row = dialogue.failure_observation(caught.exception)
            self.assertEqual(row['errorCode'], code)
            self.assertEqual(row['status'], 'partial-or-unknown')
            self.assertEqual(row['childFailure']['kind'], 'python-exception')
            self.assertEqual(dialogue.phase, 0)
            with self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
                dialogue.finish(0)

    def test_actual_nonzero_setup_returns_child_evidence_without_apply_or_error_replacement(self):
        raw = self.traceback()
        def command(*args, **kwargs):
            kwargs['dialogue'].feed(raw)
            return 1, b'SENSITIVE untrusted command body'
        with patch.object(guest, 'command', side_effect=command), patch.object(guest, 'verify_lock'), \
             patch.object(guest, 'apply_stage') as apply, patch.object(guest, 'tui_tests') as tui:
            stages, failure = guest.lifecycle(guest.environment(), b'fake-rev')
        self.assertEqual(failure, 'failed-or-unknown:setup')
        self.assertEqual(set(stages), {'setup'})
        row = stages['setup']
        self.assertEqual(row['errorCode'], 'setup-incomplete')
        self.assertEqual(row['exceptionKind'], 'GuestFailure')
        self.assertEqual(row['childFailure'], {'kind': 'python-exception', 'exceptionClass': 'KeyError',
                                               'lastKnownFrame': 'show_changes', 'invalidFieldIds': []})
        self.assertNotIn('SENSITIVE', json.dumps(row))
        apply.assert_not_called()
        tui.assert_not_called()


class InitialManifestEvaluationTests(Isolated):
    # Public fixed templates mirror log.debug's millisecond timestamp and KV order.
    prefix = b'DBG 12:34:56.789 eval: '
    started = prefix + b'Nix manifest evaluation started machine=runtime-min attr=SENSITIVE_ARGV\n'
    completed = prefix + b'Nix manifest evaluation completed machine=runtime-min elapsed=1.000s\n'
    cached = prefix + b'manifest cache hit machine=runtime-min cache=/fake/private fingerprint=SENSITIVE\n'

    def failed(self, code=b'124'):
        return (self.prefix + b'Nix manifest evaluation failed machine=runtime-min exit_code=' + code
                + b' detail=SENSITIVE https://example.invalid/FAKE_TOKEN FAKE_ENV /fake/private\n'
                b'elapsed=30.000s\n')

    def observe(self, chunks, status='unknown', code=None):
        dialogue = guest.SetupDialogue()
        for chunk in chunks:
            self.assertEqual(dialogue.feed(chunk), b'')
            self.assertLessEqual(len(dialogue.diagnostic_tail), 8192)
        row = dialogue.failure_observation(guest.GuestFailure('setup-incomplete'))
        self.assertEqual(row['initialManifestEvaluation'],
                         {'status': status, 'exitCode': code, 'debugEnabled': True})
        self.assertEqual(row['outputBytes'], sum(map(len, chunks)))
        self.assertEqual(row['outputSha256'], guest.sha(b''.join(chunks)))
        for sentinel in ('SENSITIVE', 'https://', 'FAKE_TOKEN', 'FAKE_ENV', '/fake/private', 'runtime-min'):
            self.assertNotIn(sentinel, json.dumps(row))
        return dialogue

    def test_fixed_events_full_bytewise_all_splits_csi_and_utf8(self):
        for event, status, code in ((self.started, 'started', None), (self.completed, 'completed', None),
                                    (self.cached, 'cached', None), (self.failed(), 'timeout', 124),
                                    (self.failed(b'1'), 'failed', 1), (self.failed(b'255'), 'failed', 255)):
            raw = '中文\n'.encode() + b'\x1b[2m' + event + b'\x1b[0m'
            variants = [[raw], [bytes([byte]) for byte in raw]]
            variants.extend([raw[:split], raw[split:]] for split in range(len(raw) + 1))
            for chunks in variants:
                self.observe(chunks, status, code)

    def test_rich_wrapped_fixed_message_and_parameters(self):
        for event, status in ((self.started, 'started'), (self.completed, 'completed'),
                              (self.cached, 'cached'), (self.failed(), 'timeout')):
            raw = event.replace(b' machine=', b'\r\nmachine=').replace(b' exit_code=', b'\r\nexit_code=')
            raw = raw.replace(b'manifest evaluation', b'manifest\r\nevaluation')
            self.observe([raw], status, 124 if status == 'timeout' else None)
            if status == 'timeout':
                wrapped = raw.replace(b' detail=', b'\r\ndetail=')
                self.observe([bytes([byte]) for byte in wrapped], 'timeout', 124)

    def test_wrong_scope_machine_message_timestamp_and_quoted_source(self):
        raw = self.failed()
        negatives = [raw.replace(b'eval:', b'process:'), raw.replace(b'runtime-min', b'other-machine'),
                     raw.replace(b'runtime-min', b'runtime-min-extra'), raw.replace(b'failed', b'failure'),
                     b'    print("' + raw + b'")', b'"' + raw + b'"', b'command: ' + raw]
        for timestamp in (b'12:34:56', b'12:34:56.123456', b'24:34:56.789', b'12:60:56.789',
                          b'12:34:60.789', b'1:34:56.789', b'SENSITIVE'):
            negatives.append(raw.replace(b'12:34:56.789', timestamp))
        for value in negatives:
            self.observe([value])

    def test_exit_code_bounds_suffix_and_injected_later_terminal(self):
        for code in (b'0', b'256', b'999', b'1000', b'-1', b'124suffix', b'124.0', b'True',
                     b'"124"', b'124 exit_code=1', b''):
            self.observe([self.failed(code), self.completed])

    def test_first_terminal_latches_before_detail_fake_debug_frames_and_later_evaluation(self):
        for first, status, code in ((self.failed(), 'timeout', 124), (self.failed(b'2'), 'failed', 2),
                                    (self.completed, 'completed', None), (self.cached, 'cached', None)):
            raw = (self.started + first + self.completed + self.cached + self.failed(b'3')
                   + b'  File "/fake/private", line 1, in show_changes\nKeyError: SENSITIVE\n')
            self.observe([raw], status, code)
            self.observe([bytes([byte]) for byte in raw], status, code)

    def test_traceback_header_stops_before_known_or_unknown_exception_body(self):
        for terminal in (b'KeyError', b'SENSITIVE_UNKNOWN'):
            body = (b'Traceback (most recent call last):\n'
                    b'  File "/fake/private", line 1, in _run_setup\n' + terminal + b': SENSITIVE\n'
                    + self.failed() + self.completed)
            self.observe([body])
            self.observe([self.started, body], 'started')

    def test_opaque_generic_or_wrong_machine_detail_cannot_supply_debug_evidence(self):
        for line in (b'DBG 12:34:56.789 process: command failed detail=SENSITIVE\n',
                     self.failed().replace(b'runtime-min', b'other-machine'),
                     b'detail=SENSITIVE\n'):
            self.observe([line, self.completed, self.failed()])

    def test_eight_kib_cut_line_guard_multibyte_and_lost_events(self):
        event = self.completed
        for retained in (8191, 8192, 8193):
            raw = b'SENSITIVE' * 1100 + b'\n' + event + b'x' * (retained - len(event))
            self.observe([raw])  # No first evaluation inside the bounded prefix.
        raw = b'SENSITIVE' + event + b'x' * (8192 - len(event))
        self.observe([raw])
        self.observe([bytes([byte]) for byte in raw])
        self.observe([self.failed(), b'x' * 8192], 'timeout', 124)
        self.observe([bytes([byte]) for byte in ('中文' * 1600 + '\n').encode() + event])

    def test_crop_preserves_first_terminal_whole_giant_chunk_and_bytewise(self):
        for first, status, code in ((self.failed(b'1'), 'failed', 1), (self.failed(), 'timeout', 124),
                                    (self.completed, 'completed', None), (self.cached, 'cached', None)):
            for fake in (self.completed, self.failed()):
                padding = b'x' * 8192 + b'\n'
                raw = first + padding + fake
                for chunks in ([raw], [first, padding, fake], [bytes([byte]) for byte in raw]):
                    with self.subTest(status=status, chunks=len(chunks)):
                        self.observe(chunks, status, code)

    def test_crop_without_terminal_blocks_later_fake_terminal_and_traceback(self):
        header = b'Traceback (most recent call last):\nKeyError: SENSITIVE\n'
        for first in (b'', self.started, header, self.started + header,
                      b'detail=SENSITIVE\n', self.failed(b'256')):
            raw = first + b'x' * 8192 + b'\n' + self.completed + self.failed()
            for chunks in ([raw], [first, b'x' * 8192 + b'\n', self.completed, self.failed()],
                           [bytes([byte]) for byte in raw]):
                self.observe(chunks)

    def test_event_or_header_straddling_prefix_limit_never_reopens(self):
        header = b'Traceback (most recent call last):\n'
        for marker in (self.completed, self.failed(), header):
            for retained in (1, len(marker) // 2):
                prefix = b'x' * (8192 - retained - 1) + b'\n'
                raw = prefix + marker + b'\n' + self.completed + self.failed()
                for chunks in ([raw], [prefix + marker[:retained], marker[retained:],
                                       b'\n' + self.completed + self.failed()]):
                    self.observe(chunks)

        # A complete terminal within the prefix remains evidence at the exact limit.
        prefix = b'x' * (8192 - len(self.completed) - 1) + b'\n'
        self.observe([prefix + self.completed + b'x' * 8192 + b'\n' + self.failed()], 'completed')

    def test_closed_state_is_detached_from_observation_and_has_no_raw_buffer(self):
        dialogue = self.observe([self.failed(b'1')], 'failed', 1)
        row = dialogue.failure_observation(guest.GuestFailure('setup-incomplete'))
        row['initialManifestEvaluation']['status'] = 'completed'
        dialogue.feed(b'x' * 8192 + b'\n' + self.completed)
        self.assertEqual(dialogue.failure_observation(guest.GuestFailure('setup-incomplete'))[
            'initialManifestEvaluation'], {'status': 'failed', 'exitCode': 1, 'debugEnabled': True})
        baseline = {'text', 'diagnostic_tail', 'diagnostic_line_start', 'wire', 'alternate', 'answered',
                    'decoder', 'phase', 'saved', 'existing', 'checkout', 'output_bytes', 'output_digest'}
        extra = {key: value for key, value in vars(dialogue).items() if key not in baseline}
        self.assertEqual(extra, {'initial_evaluation': {'status': 'failed', 'exitCode': 1, 'debugEnabled': True},
                                 'initial_evaluation_closed': True})

    def test_debug_observation_keeps_unknown_prompt_and_success_guards(self):
        for raw, code in ((b'SENSITIVE [y/n]', 'unknown-pty-prompt'),
                          (b'Apply changes and save? [y/n]', 'pty-stage-order')):
            dialogue = guest.SetupDialogue()
            dialogue.feed(self.failed())
            with self.assertRaisesRegex(guest.GuestFailure, code) as caught:
                dialogue.feed(raw)
            row = dialogue.failure_observation(caught.exception)
            self.assertEqual(row['errorCode'], code)
            self.assertEqual(row['initialManifestEvaluation']['status'], 'timeout')
            self.assertEqual(dialogue.phase, 0)
            with self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
                dialogue.finish(0)

    def test_setup_only_env_copy_preserves_parent_production_argv_and_other_commands(self):
        for inherited in (None, '0', '1'):
            env = guest.environment()
            if inherited is not None:
                env['ENVY_DEBUG'] = inherited
            before = dict(env)
            parent = {'SENSITIVE_PARENT': 'SENSITIVE'}
            with patch.dict(guest.os.environ, parent, clear=True), \
                 patch.object(guest, 'command', return_value=(1, b'')) as command, \
                 patch.object(guest, 'verify_lock') as lock:
                with self.assertRaisesRegex(guest.GuestFailure, 'setup-incomplete'):
                    guest.setup_stage(env, b'fake-rev')
                self.assertEqual(dict(guest.os.environ), parent)
            self.assertEqual(env, before)
            child = command.call_args.kwargs['env']
            self.assertIsNot(child, env)
            self.assertEqual(child, {**before, 'ENVY_DEBUG': '1'})
            self.assertEqual(command.call_args.args, (['/bin/bash', str(guest.REPO / 'install.sh'),
                '--mirror', 'upstream', '--target', str(guest.REPO)],))
            self.assertEqual({k: v for k, v in command.call_args.kwargs.items() if k not in ('env', 'dialogue')},
                {'cwd': str(guest.HOME), 'terminal': True, 'timeout': 1800})
            lock.assert_called_once_with(env)
        for user in (False, True):
            with patch.dict(guest.os.environ, {'ENVY_DEBUG': '1'}, clear=True):
                self.assertNotIn('ENVY_DEBUG', guest.environment(user=user))


class PayloadTests(Isolated):
    def test_metadata_reference_closure_self_reference_and_private_fields(self):
        meta = metadata()
        self.assertEqual(guest.validate_metadata(meta), {store('source')})
        for change in ('missing-ref', 'duplicate', 'raw', 'source-hash'):
            bad = copy.deepcopy(meta)
            if change == 'missing-ref':
                bad['preparation']['objects'][0]['references'] = [store('missing')]
            elif change == 'duplicate':
                bad['preparation']['objects'] *= 2
            elif change == 'raw':
                bad['preparation']['objects'][0]['agePrivateKey'] = 'DO-NOT-PRINT'
            else:
                bad['source']['narHash'] = sri(b'other')
            with self.subTest(change=change), self.assertRaises(guest.GuestFailure):
                guest.validate_metadata(bad)

    def archive(self, extra=None):
        files = {'flake.lock': b'{}', 'setup.py': b'pass\n'}
        manifest = {'schemaVersion': 1, 'files': [
            {'relativePath': name, 'mode': '0644', 'sha256': guest.sha(data), 'origin': 'working-tree'}
            for name, data in files.items()], 'lockSha256': guest.sha(files['flake.lock']),
            'fixture': {'selectionStage': 'minimal', 'secrets': 'guest-generated-only'}}
        files['snapshot-manifest.json'] = json.dumps(manifest).encode()
        meta = metadata()
        meta['source']['manifestSha256'] = guest.sha(files['snapshot-manifest.json'])
        meta['source']['lockSha256'] = manifest['lockSha256']
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as archive:
            for name, data in files.items():
                row = tarfile.TarInfo('source/' + name)
                row.mode, row.size = 0o644, len(data)
                archive.addfile(row, io.BytesIO(data))
            if extra:
                row = tarfile.TarInfo(extra)
                row.mode, row.size = 0o644, 1
                archive.addfile(row, io.BytesIO(b'x'))
        return out.getvalue(), meta

    def test_source_tar_hash_inventory_and_private_metadata_rejected_before_extraction(self):
        raw, meta = self.archive()
        files = guest.source_members(raw, meta)
        self.assertEqual(set(files), {'source/flake.lock', 'source/setup.py', 'source/snapshot-manifest.json'})
        for name in ('source/.device-label', 'source/.git/config', 'source/docs/plan.md', 'source/.github/workflows/a.yml',
                     'source/hosts/linux/actual-host.nix', 'source/secrets/secrets.yaml', 'source/../../home/history',
                     'source/resources/unlisted.py'):
            raw, meta = self.archive(name)
            with self.subTest(name=name), self.assertRaises(guest.GuestFailure):
                guest.source_members(raw, meta)
        raw, meta = self.archive()
        meta['source']['manifestSha256'] = '0' * 64
        with self.assertRaises(guest.GuestFailure):
            guest.source_members(raw, meta)

    def test_import_uses_one_fd_scoped_signature_override_native_metadata_and_content_verify(self):
        meta, calls = metadata(), []
        item = meta['preparation']['objects'][0]
        def nix(args, env, **kwargs):
            calls.append(args)
            if args[0] == 'path-info':
                return json.dumps([item]).encode()
            if args[:2] == ['flake', 'archive']:
                return json.dumps({'path': store('source'), 'inputs': {}}).encode()
            return b''
        with patch.object(guest, 'checked', return_value=(store('source') + '\n').encode()) as check, patch.object(guest, 'nix', side_effect=nix):
            guest.import_payload(77, meta, {})
        self.assertEqual(check.call_args.kwargs['input_fd'], 77)
        self.assertEqual(check.call_args.args[0].count('require-sigs'), 1)
        self.assertIn('false', check.call_args.args[0])
        self.assertEqual(calls[1], ['store', 'verify', '--no-trust', '--no-substitute', store('source')])
        self.assertNotIn('require-sigs', guest.environment()['NIX_CONFIG'])
        self.assertEqual(len(calls), 3)
        wrong = {**item, 'narSize': 101}
        with patch.object(guest, 'checked', return_value=(store('source') + '\n').encode()), \
             patch.object(guest, 'nix', return_value=json.dumps([wrong]).encode()) as query:
            with self.assertRaisesRegex(guest.GuestFailure, 'native-object-mismatch'):
                guest.import_payload(77, meta, {})
            self.assertEqual(query.call_count, 1)


class SerialTests(Isolated):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))
        self.opened = self.enterContext(patch.object(guest.os, 'open', return_value=71))
        self.info = self.enterContext(patch.object(guest.os, 'fstat', return_value=SimpleNamespace(
            st_mode=stat.S_IFCHR | 0o600, st_rdev=os.makedev(4, 64))))
        self.closed = self.enterContext(patch.object(guest.os, 'close'))
        self.written = self.enterContext(patch.object(guest.os, 'write', side_effect=lambda fd, data: len(data)))
        self.wait = self.enterContext(patch.object(guest.time, 'sleep'))

    def test_closed_prefixes_have_exact_undecorated_json_frames_and_fd_only_io(self):
        for prefix in guest.SERIAL_PREFIXES:
            self.opened.reset_mock()
            self.written.reset_mock()
            self.closed.reset_mock()
            row = {'token': TOKEN, 'fact': 'synthetic'}
            EMIT_SERIAL(prefix, row)
            self.opened.assert_called_once_with('/dev/ttyS0', os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            self.info.assert_called_with(71)
            self.closed.assert_called_once_with(71)
            self.assertEqual(self.written.call_args.args,
                             (71, (prefix + json.dumps(row, sort_keys=True) + '\n').encode()))

    def test_split_short_writes_eagain_and_eintr_preserve_all_bytes(self):
        output = bytearray()
        responses = iter([3, BlockingIOError(errno.EAGAIN, 'synthetic'),
                          InterruptedError(errno.EINTR, 'synthetic'), 1, 7])
        def write(fd, data):
            self.assertEqual(fd, 71)
            self.assertLessEqual(len(data), 8192)
            response = next(responses, len(data))
            if isinstance(response, Exception):
                raise response
            output.extend(data[:response])
            return response
        self.written.side_effect = write
        row = {'token': TOKEN, 'payload': 'x' * 20000}
        EMIT_SERIAL(guest.PREFIX, row)
        self.assertEqual(bytes(output), (guest.PREFIX + json.dumps(row, sort_keys=True) + '\n').encode())
        self.wait.assert_called_once_with(0.01)
        self.opened.assert_called_once()
        self.closed.assert_called_once_with(71)

    def test_serial_budget_includes_prefix_and_newline_and_refuses_before_open(self):
        overhead = len((guest.PREFIX + json.dumps({'x': ''}, sort_keys=True) + '\n').encode())
        row = {'x': 'a' * (guest.MAX_SERIAL - overhead)}
        EMIT_SERIAL(guest.PREFIX, row)
        self.assertEqual(sum(len(call.args[1]) for call in self.written.call_args_list), guest.MAX_SERIAL)
        self.opened.reset_mock()
        self.written.reset_mock()
        row['x'] += 'a'
        with self.assertRaisesRegex(guest.GuestFailure, 'serial-budget'):
            EMIT_SERIAL(guest.PREFIX, row)
        self.opened.assert_not_called()
        self.written.assert_not_called()

    def test_nonchar_directory_and_wrong_rdev_refuse_on_held_fd(self):
        for mode, device in ((stat.S_IFREG, os.makedev(4, 64)), (stat.S_IFDIR, os.makedev(4, 64)),
                             (stat.S_IFCHR, os.makedev(4, 65)), (stat.S_IFCHR, os.makedev(5, 64))):
            self.closed.reset_mock()
            self.info.return_value = SimpleNamespace(st_mode=mode, st_rdev=device)
            with self.subTest(mode=mode, device=device), self.assertRaisesRegex(guest.GuestFailure, 'serial-device'):
                EMIT_SERIAL(guest.PREFIX, {})
            self.closed.assert_called_once_with(71)
        self.written.assert_not_called()

    def test_symlink_open_failure_never_reopens_writes_or_closes_unowned_fd(self):
        self.opened.side_effect = OSError(errno.ELOOP, 'synthetic symlink')
        with self.assertRaises(OSError):
            EMIT_SERIAL(guest.PREFIX, {})
        self.opened.assert_called_once_with('/dev/ttyS0', os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        self.info.assert_not_called()
        self.closed.assert_not_called()
        self.written.assert_not_called()

    def test_invalid_prefix_and_user_uid_refuse_before_open(self):
        for prefix in ('ENVY_RUNTIME_GUEST_ABORT ', 'arbitrary', None, []):
            with self.subTest(prefix=prefix), self.assertRaisesRegex(guest.GuestFailure, 'serial-prefix'):
                EMIT_SERIAL(prefix, {})
        for uid, euid in ((1000, 1000), (1000, 0), (0, 1000)):
            with patch.object(guest.os, 'getuid', return_value=uid), \
                 patch.object(guest.os, 'geteuid', return_value=euid), self.assertRaisesRegex(guest.GuestFailure, 'serial-uid'):
                EMIT_SERIAL(guest.PREFIX, {})
        self.opened.assert_not_called()

    def test_deadline_bounds_both_eagain_and_continuous_partial_progress(self):
        for result in (1, BlockingIOError(errno.EAGAIN, 'synthetic')):
            self.closed.reset_mock()
            with patch.object(guest.time, 'monotonic', side_effect=[1, 1, 7]):
                self.written.side_effect = result if isinstance(result, Exception) else lambda fd, data: 1
                with self.assertRaisesRegex(guest.GuestFailure, 'serial-timeout'):
                    EMIT_SERIAL(guest.PREFIX, {})
            self.closed.assert_called_once_with(71)

    def test_zero_invalid_write_and_io_failure_close_held_fd(self):
        for result in (0, -1, True, 9000, OSError(errno.EIO, 'synthetic')):
            self.closed.reset_mock()
            self.written.side_effect = result if isinstance(result, Exception) else None
            self.written.return_value = result
            with self.subTest(result=result), self.assertRaises((guest.GuestFailure, OSError)):
                EMIT_SERIAL(guest.PREFIX, {})
            self.closed.assert_called_once_with(71)


class RootDiagnosticTests(Isolated):
    def main_failure(self, error, *, phase='source-add', args=()):
        def fail(config):
            guest.ROOT_PHASE = phase
            raise error
        with patch.object(guest.sys, 'argv', [guest.SCRIPT, *args]), patch.object(guest.signal, 'signal'), \
             patch.object(guest, 'guard'), patch.object(guest, 'root_stages', side_effect=fail), \
             patch.object(guest, 'user_stages', side_effect=fail), patch.object(guest, 'checked') as poweroff, \
             patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as stdout:
            result = guest.main(CONFIG)
        self.assertIsNone(result)
        return poweroff, stdout.getvalue()

    def test_known_safe_codes_are_nonce_bound_without_exception_or_environment_leaks(self):
        fact = {'exitCode': 97, 'outputBytes': 10, 'outputSha256': 'a' * 64}
        guest.COMMAND_FACTS.extend([fact] * 10)
        for code in ('native-source-path', 'native-source-nar', 'native-info', 'archive-source',
                     'lock-drift', 'command-failed', 'command-timeout'):
            self.serial.reset_mock()
            with patch.dict(guest.os.environ, {'SYNTHETIC_SECRET': '/private/SYNTHETIC-PRIVATE'}):
                poweroff, output = self.main_failure(guest.GuestFailure(code))
            self.assertEqual(output, '')
            self.serial.assert_called_once_with(guest.DIAGNOSTIC_PREFIX, {
                'schemaVersion': 1, 'token': TOKEN, 'phase': 'source-add', 'error': code, 'lastCommands': [fact] * 8})
            poweroff.assert_called_once()
            self.assertEqual(poweroff.call_args.args[0], ['/usr/bin/systemctl', 'poweroff', '--no-block'])
            self.assertNotIn('SYNTHETIC', json.dumps(self.serial.call_args.args))

    def test_unknown_nonstring_and_os_errors_never_serialize_raw_exception(self):
        errors = (guest.GuestFailure('/private/SYNTHETIC-PRIVATE'), guest.GuestFailure('unexpected-code'),
                  guest.GuestFailure([]), guest.GuestFailure({'secret': 'SYNTHETIC-PRIVATE'}),
                  guest.GuestFailure('native-source-path', 'SYNTHETIC-PRIVATE'),
                  OSError(errno.EIO, 'SYNTHETIC-PRIVATE', '/private/secret'), ValueError('SYNTHETIC-PRIVATE'))
        for error in errors:
            self.serial.reset_mock()
            poweroff, output = self.main_failure(error)
            row = self.serial.call_args.args[1]
            self.assertEqual(row['error'], 'unknown')
            self.assertNotIn('SYNTHETIC', json.dumps(row) + output)
            self.assertNotIn('/private', json.dumps(row) + output)
            poweroff.assert_called_once()

    def test_untrusted_fact_values_and_phase_are_filtered(self):
        valid = {'exitCode': 1, 'outputBytes': 10, 'outputSha256': 'a' * 64}
        guest.COMMAND_FACTS.extend([valid, {**valid, 'argv': '/private/SYNTHETIC'},
            {**valid, 'exitCode': True}, {**valid, 'outputBytes': -1},
            {**valid, 'outputSha256': 'SYNTHETIC-PRIVATE'}, '/private/SYNTHETIC'])
        self.main_failure(guest.GuestFailure('command-failed'), phase='/private/SYNTHETIC')
        row = self.serial.call_args.args[1]
        self.assertEqual(row['phase'], 'root-stages')
        self.assertEqual(row['lastCommands'], [valid])
        self.assertNotIn('SYNTHETIC', json.dumps(row))

    def test_diagnostic_timeout_or_io_failure_cannot_mask_cleanup(self):
        for error in (guest.GuestFailure('serial-timeout'), OSError(errno.EIO, 'SYNTHETIC'), KeyboardInterrupt()):
            self.serial.side_effect = error
            poweroff, output = self.main_failure(guest.GuestFailure('native-source-path'))
            poweroff.assert_called_once()
            self.assertEqual(output, '')

    def test_root_cancellation_diagnoses_and_user_failure_never_writes_serial(self):
        self.main_failure(KeyboardInterrupt(), phase='flake-archive')
        self.assertEqual(self.serial.call_args.args[1]['error'], 'cancelled')
        for args in (('user',), ('lifecycle',)):
            self.serial.reset_mock()
            poweroff, output = self.main_failure(guest.GuestFailure('native-source-path'), args=args)
            self.serial.assert_not_called()
            poweroff.assert_not_called()
            self.assertEqual(output, 'ENVY_RUNTIME_GUEST_FAILURE failed-or-unknown\n')

    def test_guard_rejection_has_no_diagnostic_or_poweroff(self):
        with patch.object(guest.sys, 'argv', [guest.SCRIPT]), patch.object(guest.signal, 'signal'), \
             patch.object(guest, 'guard', side_effect=guest.GuestFailure('guest-root-mount')), \
             patch.object(guest, 'checked') as poweroff, self.assertRaises(guest.GuestFailure):
            guest.main(CONFIG)
        self.serial.assert_not_called()
        poweroff.assert_not_called()

    def test_fetch_phase_records_exact_closed_operation_without_native_calls(self):
        meta = network_metadata()
        env = guest.environment(transport='guest-fetch')
        actual = {'path': store('source'), 'narHash': meta['source']['narHash'], 'narSize': 100, 'references': []}
        prefix = [(store('source') + '\n').encode(), json.dumps([actual]).encode()]
        for index, phase in enumerate(('source-add', 'source-info', 'flake-archive')):
            with patch.object(guest, 'nix', side_effect=prefix[:index] + [guest.GuestFailure('command-failed')]), \
                 patch.object(guest, 'verify_lock'), self.assertRaises(guest.GuestFailure):
                guest.fetch_payload(meta, env, Path('/synthetic/source'), network_config(meta))
            self.assertEqual(guest.ROOT_PHASE, phase)

    def test_bootstrap_phases_distinguish_setup_and_locked_tools_without_native_calls(self):
        meta = network_metadata()
        env = guest.environment(transport='guest-fetch')
        for responses, phase in (([guest.GuestFailure('command-failed')], 'setup-build'),
                                  ([b'', guest.GuestFailure('command-failed')], 'locked-tools')):
            with patch.object(guest, 'nix', side_effect=responses), self.assertRaises(guest.GuestFailure):
                guest.bootstrap_tools(meta, env)
            self.assertEqual(guest.ROOT_PHASE, phase)


class RootStagesTests(Isolated):
    def test_root_transport_install_import_login_and_receipt_use_actual_order(self):
        meta = metadata()
        fixture = receipt_fixtures.ReceiptTests()
        fixture.setUp()
        row = copy.deepcopy(fixture.row)
        row['source'] = meta['source']
        row['install'] = None
        facts = {'token': TOKEN, 'commandCount': 1, 'lastCommands': [
            {'exitCode': 97, 'outputBytes': 10, 'outputSha256': 'a' * 64}]}
        tools = {name: store('source') for name in ('gitMinimal', 'sops', 'age', 'ssh-to-age')}
        events, serial_bytes = [], bytearray()
        self.serial.side_effect = EMIT_SERIAL
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'open', return_value=71))
        self.enterContext(patch.object(guest.os, 'fstat', return_value=SimpleNamespace(
            st_mode=stat.S_IFCHR, st_rdev=os.makedev(4, 64))))
        def serial_write(fd, data):
            self.assertEqual(fd, 71)
            serial_bytes.extend(data)
            return len(data)
        self.enterContext(patch.object(guest.os, 'write', side_effect=serial_write))
        def checked(argv, **kwargs):
            events.append(argv[0])
            return b''
        self.enterContext(patch.object(guest, 'configure_daemon_ca', side_effect=AssertionError('offline daemon effect')))
        self.enterContext(patch.object(guest, 'native_tls_probe', side_effect=AssertionError('offline native TLS effect')))
        with patch.object(guest, 'guard') as guard, patch.object(guest.os.path, 'lexists', return_value=False), \
             patch.object(Path, 'mkdir'), patch.object(guest.os, 'read', return_value=json.dumps(meta).encode()), \
             patch.object(guest, 'payload_fd', side_effect=[10, 11, 12]), patch.object(guest.os, 'close'), \
             patch.object(guest.os, 'dup', return_value=11), patch.object(guest.os, 'fdopen', return_value=io.BytesIO(b'tar')), \
             patch.object(guest, 'source_members', return_value={'source/setup.py': (b'pass', 0o644)}), \
             patch.object(guest, 'install_official', side_effect=lambda *a: events.append('official') or fixture.installed), \
             patch.object(guest, 'import_payload', side_effect=lambda *a: events.append('import')), \
             patch.object(guest, 'bootstrap_tools', return_value=tools), patch.object(guest, 'write_new'), \
             patch.object(guest.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000)), \
             patch.object(guest.os, 'walk', return_value=[]), patch.object(guest.os, 'chown'), \
             patch.object(guest, 'checked', side_effect=checked) as check, patch.object(guest, 'stop_user_unit') as stop, \
             patch.object(guest, 'private_read', side_effect=[json.dumps(row).encode(), json.dumps(facts).encode()]), \
             patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as stdout:
            result = guest.root_stages(CONFIG)
            raw = bytes(serial_bytes)
            self.assertEqual(stdout.getvalue(), '')
            self.assertEqual([call.args[0] for call in self.serial.call_args_list],
                             [guest.INSTALL_PREFIX, guest.OBSERVATIONS_PREFIX, guest.PREFIX])
        self.assertEqual(events[:3], ['/usr/bin/mount', 'official', 'import'])
        self.assertEqual(guard.call_count, 2)
        self.assertEqual(result['install'], fixture.installed)
        self.assertEqual(raw.count(guest.INSTALL_PREFIX.encode()), 1)
        self.assertEqual(len(raw.splitlines()), 3)
        self.assertEqual([line.split(b' ', 1)[0] for line in raw.splitlines()],
                         [prefix.strip().encode() for prefix in
                          (guest.INSTALL_PREFIX, guest.OBSERVATIONS_PREFIX, guest.PREFIX)])
        self.assertEqual(acceptance.receipt_from(raw, TOKEN, meta['source']), result)
        login = check.call_args_list[-1]
        self.assertTrue(login.kwargs['terminal'])
        self.assertIn('--property=PAMName=login', login.args[0])
        self.assertIn('--property=KillMode=control-group', login.args[0])
        self.assertNotIn('XDG_RUNTIME_DIR', ' '.join(login.args[0]))
        stop.assert_called_once()

    def test_seed_metadata_uses_production_public_recipient_writer_and_valid_label(self):
        storage = SimpleNamespace(current_device_public_key=Mock(return_value='age1syntheticpublic'),
                                  write_sops_yaml_keys=Mock())
        identity = SimpleNamespace(set_sops_label=Mock())
        with patch.dict(sys.modules, {'envy.keys.storage': storage, 'envy.keys.identity': identity}), \
             patch.object(guest.sys, 'path', list(sys.path)), \
             patch.object(guest.os, 'open', side_effect=AssertionError('direct key reads forbidden')):
            guest.normalize_seed_identity()
        storage.write_sops_yaml_keys.assert_called_once_with({'runtime_fixture': 'age1syntheticpublic'})
        identity.set_sops_label.assert_called_once_with('runtime_fixture')
        storage.current_device_public_key.return_value = None
        storage.write_sops_yaml_keys.reset_mock()
        with patch.dict(sys.modules, {'envy.keys.storage': storage, 'envy.keys.identity': identity}), \
             patch.object(guest.sys, 'path', list(sys.path)), self.assertRaises(guest.GuestFailure):
            guest.normalize_seed_identity()
        storage.write_sops_yaml_keys.assert_not_called()

    def test_cleanup_has_own_deadline_and_uses_fixed_systemd_unit(self):
        observed = []
        def command(argv, **kwargs):
            observed.append((argv, guest.DEADLINE))
            return 5, b'unit already collected'
        with patch.object(guest, 'DEADLINE', 0), patch.object(guest, 'command', side_effect=command):
            guest.stop_user_unit({})
            self.assertEqual(guest.DEADLINE, 0)
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(deadline > 1 for argv, deadline in observed))
        self.assertEqual(observed[0][0][-1], 'envy-runtime-lifecycle.service')
        self.assertIn('--kill-whom=all', observed[0][0])

    def test_user_reentry_requires_bound_metadata_and_uses_real_runtime_interpreter(self):
        meta = metadata()
        tools = {name: store('source') for name in ('gitMinimal', 'sops', 'age', 'ssh-to-age')}
        context = json.dumps({'metadata': meta, 'tools': tools}).encode()
        config = {**CONFIG, 'metadataSha256': guest.sha((json.dumps(meta, sort_keys=True) + '\n').encode())}
        output, python = store('setup'), store('python3-env')
        wrapper = ('export PATH="' + python + '/bin:$PATH"').encode()
        def nix(args, env, **kwargs):
            if '--json' in args and args[0] == 'build':
                return json.dumps([{'outputs': {'out': output}}]).encode()
            return b'{}'
        with patch.object(guest, 'guard'), patch.object(guest, 'private_read', side_effect=[context, wrapper]), \
             patch.dict(guest.os.environ, {}, clear=True), patch.object(guest, 'checked', return_value=b'active'), \
             patch.object(guest, 'prepare_checkout') as prepare, patch.object(guest, 'nix', side_effect=nix), \
             patch.object(Path, 'is_file', return_value=True), patch.object(guest.sys, 'argv', [guest.SCRIPT, 'user']), \
             patch.object(guest.os, 'execve', side_effect=KeyboardInterrupt) as execute:
            with self.assertRaises(KeyboardInterrupt):
                guest.user_stages(config)
        prepare.assert_called_once()
        self.assertEqual(execute.call_args.args[:2], (python + '/bin/python3', [python + '/bin/python3', guest.SCRIPT, 'lifecycle']))
        self.assertNotIn('ENVY_DEV_SHELL', execute.call_args.args[2])
        self.assertNotIn('XDG_RUNTIME_DIR', execute.call_args.args[2])



class ProcessTests(Isolated):
    def fake_process(self):
        process = Mock(pid=424242)
        process.stdout.fileno.return_value = 99
        process.poll.side_effect = AssertionError('early reap forbidden')
        self.waitid.side_effect = None
        self.waitid.return_value = SimpleNamespace(si_pid=424242, si_code=guest.os.CLD_EXITED, si_status=0)
        return process

    def run_nix_pipe(self, stderr, *, stdout=b'{}', code=1, argv=None, merge=False, **kwargs):
        process = self.fake_process()
        process.stderr.fileno.return_value = 100
        self.waitid.return_value = SimpleNamespace(si_pid=424242, si_code=guest.os.CLD_EXITED, si_status=code)
        chunks = {99: iter([stdout, b'']), 100: iter([*stderr, b''])}
        def ready(fds, *args):
            return list(fds), [], []
        with patch.object(guest.subprocess, 'Popen', return_value=process), patch.object(guest, 'stop'), \
             patch.object(guest.select, 'select', side_effect=ready), \
             patch.object(guest.os, 'read', side_effect=lambda fd, size: next(chunks[fd])):
            return guest.command(argv or [guest.NIX], env={}, merge=merge, **kwargs)

    def test_nix_failure_static_categories_and_unknown_fallback(self):
        cases = (
            (b'SSL certificate verification failed', 'nix-certificate'),
            (b'self-signed certificate in certificate chain', 'nix-certificate'),
            (b'unable to get local issuer certificate', 'nix-certificate'),
            (b'Could not resolve host: private.invalid', 'nix-dns'),
            (b'API rate limit exceeded for SYNTHETIC-SECRET', 'nix-github-rate-limit'),
            (b'GitHub API rate limit exceeded; HTTP error 403', 'nix-github-rate-limit'),
            (b'HTTP error 403', 'nix-http-403'), (b'HTTP/2 401', 'nix-http-401'),
            (b'HTTP status code: 404', 'nix-http-404'),
            (b'Failed to connect to private.invalid', 'nix-connection'),
            (b'operation timed out', 'nix-connection'),
            (b'hash mismatch in fixed-output derivation', 'nix-hash-mismatch'),
            (b"unrecognised flag '--private'", 'nix-unsupported-option'),
            (b"unknown option '--private'", 'nix-unsupported-option'),
            (b"executing 'git': No such file or directory", 'nix-missing-git'),
            (b'git: command not found', 'nix-missing-git'),
            (b"syntax error, unexpected token in /private/source", 'nix-evaluation'),
            (b"attribute 'SYNTHETIC-SECRET' missing", 'nix-evaluation'),
            (b'HTTP 4030 certificate SYNTHETIC-SECRET', 'unknown'),
            (b'unknown private exception /private/secret', 'unknown'),
        )
        for raw, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(guest.classify_nix_failure(raw), expected)
                self.assertIn(expected, guest.DIAGNOSTIC_CODES)

    def test_actual_split_stderr_only_emits_static_nonce_bound_diagnostic(self):
        guest.ROOT_PHASE = 'flake-archive'
        secret = b'SYNTHETIC-SECRET /private/credential?token=private'
        for chunks, expected in (([b'SSL certifi', b'cate verification failed ', secret], 'nix-certificate'),
                                 ([b'API rate li', b'mit exceeded ', secret], 'nix-github-rate-limit'),
                                 ([secret, b' arbitrary failure'], 'command-failed')):
            self.serial.reset_mock()
            with patch.object(guest.os, 'getuid', return_value=0), \
                 patch.object(guest.os, 'geteuid', return_value=0), \
                 patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as stdout:
                process = self.fake_process()
                process.stderr.fileno.return_value = 100
                self.waitid.return_value = SimpleNamespace(si_pid=424242, si_code=guest.os.CLD_EXITED, si_status=1)
                pipes = {99: iter([b'']), 100: iter([*chunks, b''])}
                with patch.object(guest.subprocess, 'Popen', return_value=process), patch.object(guest, 'stop'), \
                     patch.object(guest.select, 'select', side_effect=lambda fds, *a: (list(fds), [], [])), \
                     patch.object(guest.os, 'read', side_effect=lambda fd, size: next(pipes[fd])):
                    with self.assertRaises(guest.GuestFailure) as caught:
                        guest.checked([guest.NIX], env={}, merge=False)
                guest.emit_diagnostic(CONFIG, caught.exception)
                self.assertEqual(stdout.getvalue(), '')
            row = self.serial.call_args.args[1]
            self.assertEqual(row['error'], expected)
            self.assertEqual(row['token'], TOKEN)
            self.assertEqual(set(row), {'schemaVersion', 'token', 'phase', 'error', 'lastCommands'})
            self.assertNotIn('SYNTHETIC', json.dumps(row))
            self.assertNotIn('/private', json.dumps(row))
            self.assertEqual(guest.COMMAND_FACTS[-1]['outputBytes'], sum(map(len, chunks)))

    def test_nix_classifier_scope_excludes_stdout_success_user_and_other_commands(self):
        cases = ((0, 0, 'flake-archive', guest.NIX, False, 1, b'{}'),
                 (0, 0, 'setup-build', guest.NIX, False, 1, b'{}'),
                 (0, 0, 'locked-tools', guest.NIX, False, 1, b'{}'),
                 (1000, 1000, 'flake-archive', guest.NIX, False, 1, b'{}'),
                 (0, 1000, 'flake-archive', guest.NIX, False, 1, b'{}'),
                 (0, 0, 'user-session', guest.NIX, False, 1, b'{}'),
                 (0, 0, 'root-stages', guest.NIX, False, 1, b'{}'),
                 (0, 0, 'unreviewed', guest.NIX, False, 1, b'{}'),
                 (0, 0, 'flake-archive', '/fixed/other', False, 1, b'{}'),
                 (0, 0, 'flake-archive', guest.NIX, True, 1, b'certificate verification failed'),
                 (0, 0, 'flake-archive', guest.NIX, False, 0, b'{}'))
        for uid, euid, phase, argv0, merge, code, output in cases:
            guest.ROOT_PHASE = phase
            guest.LAST_NIX_FAILURE = 'nix-http-403'
            with self.subTest(case=(uid, euid, phase, argv0, merge, code)), \
                 patch.object(guest.os, 'getuid', return_value=uid), \
                 patch.object(guest.os, 'geteuid', return_value=euid), \
                 patch.object(guest, 'classify_nix_failure', wraps=guest.classify_nix_failure) as classify:
                result = self.run_nix_pipe([b'certificate verification failed'], stdout=output,
                                           code=code, argv=[argv0], merge=merge)
            enabled = uid == euid == 0 and phase in guest.NIX_FAILURE_PHASES and argv0 == guest.NIX and not merge and code != 0
            self.assertEqual(classify.call_count, int(enabled))
            self.assertEqual(guest.LAST_NIX_FAILURE, 'nix-certificate' if enabled else 'unknown')
            self.assertEqual(result, (code, output))
        guest.ROOT_PHASE = 'flake-archive'
        with patch.object(guest.os, 'getuid', return_value=0), patch.object(guest.os, 'geteuid', return_value=0):
            self.run_nix_pipe([b'unknown failure'], stdout=b'certificate verification failed')
        self.assertEqual(guest.LAST_NIX_FAILURE, 'unknown')

    def test_nix_stderr_sample_cap_total_budget_digest_and_short_lifetime(self):
        guest.ROOT_PHASE = 'flake-archive'
        stderr = [b'API rate li', b'mit exceeded', b'x' * 8192, b'certificate verification failed']
        retained = []
        actual = guest.classify_nix_failure
        def classify(sample):
            retained.append(sample)
            self.assertEqual(len(sample), guest.MAX_NIX_STDERR)
            return actual(sample)
        with patch.object(guest.os, 'getuid', return_value=0), patch.object(guest.os, 'geteuid', return_value=0), \
             patch.object(guest, 'classify_nix_failure', side_effect=classify):
            self.assertEqual(self.run_nix_pipe(stderr), (1, b'{}'))
        self.assertEqual(guest.LAST_NIX_FAILURE, 'nix-github-rate-limit')
        self.assertEqual(retained, [bytearray()])
        self.assertEqual(guest.classify_nix_failure(b'x' * 8192 + b'certificate verification failed'), 'unknown')
        raw = b'{}' + b''.join(stderr)
        self.assertEqual(guest.COMMAND_FACTS[-1], {'exitCode': 1, 'outputBytes': len(raw), 'outputSha256': guest.sha(raw)})
        with patch.object(guest.os, 'getuid', return_value=0), patch.object(guest.os, 'geteuid', return_value=0), \
             patch.object(guest, 'MAX_OUTPUT', 16), patch.object(guest, 'classify_nix_failure') as classify:
            with self.assertRaisesRegex(guest.GuestFailure, 'command-output-limit'):
                self.run_nix_pipe([b'x' * 17])
            classify.assert_not_called()
        self.assertEqual(guest.LAST_NIX_FAILURE, 'unknown')

    def test_checked_mock_reset_prevents_stale_hint_and_untrusted_codes(self):
        guest.LAST_NIX_FAILURE = 'nix-certificate'
        with patch.object(guest, 'command', return_value=(1, b'SYNTHETIC-SECRET')):
            with self.assertRaisesRegex(guest.GuestFailure, '^command-failed$'):
                guest.checked([guest.NIX], env={})
        self.assertEqual(guest.LAST_NIX_FAILURE, 'unknown')
        def fake(*args, **kwargs):
            guest.LAST_NIX_FAILURE = '/private/SYNTHETIC-SECRET'
            return 1, b''
        with patch.object(guest, 'command', side_effect=fake):
            with self.assertRaisesRegex(guest.GuestFailure, '^command-failed$'):
                guest.checked([guest.NIX], env={})

    def test_timeout_cancellation_and_output_limit_kill_only_owned_process(self):
        for reason in ('timeout', 'cancel', 'output'):
            process = self.fake_process()
            with self.subTest(reason=reason), patch.object(guest.subprocess, 'Popen', return_value=process), \
                 patch.object(guest, 'stop') as stop, patch.object(guest.select, 'select', return_value=([99], [], [])), \
                 patch.object(guest.os, 'read', return_value=b'x' * (guest.MAX_OUTPUT + 1)), \
                 patch.object(guest, 'tick', side_effect=[None, KeyboardInterrupt()] if reason == 'cancel' else None), \
                 patch.object(guest.time, 'monotonic', side_effect=[1, 20] if reason == 'timeout' else None, return_value=1):
                with self.assertRaises((guest.GuestFailure, KeyboardInterrupt)):
                    guest.command(['/fixed/fake'], env={}, timeout=10)
                stop.assert_called_once_with(424242)
                process.wait.assert_called_once_with(timeout=3)
                process.stdout.close.assert_called_once()

    def test_json_stdout_is_not_polluted_by_bounded_build_progress_stderr(self):
        process = self.fake_process()
        process.stderr.fileno.return_value = 100
        self.waitid.return_value = SimpleNamespace(si_pid=424242, si_code=guest.os.CLD_EXITED, si_status=0)
        chunks = {99: iter([b'{"built": true}', b'']), 100: iter([b'building progress\n', b''])}
        with patch.object(guest.subprocess, 'Popen', return_value=process), patch.object(guest, 'stop'), \
             patch.object(guest.select, 'select', return_value=([99, 100], [], [])), \
             patch.object(guest.os, 'read', side_effect=lambda fd, size: next(chunks[fd])):
            code, raw = guest.command(['/fixed/nix'], env={}, merge=False)
        self.assertEqual(json.loads(raw), {'built': True})
        self.assertEqual(code, 0)
        self.assertGreater(guest.COMMAND_FACTS[-1]['outputBytes'], len(raw))
        process.stderr.close.assert_called_once()
        with patch.object(guest, 'checked', return_value=b'{}') as checked:
            guest.nix(['eval', '--json', 'fixed'], {})
        self.assertFalse(checked.call_args.kwargs['merge'])

    def test_output_is_observed_but_not_echoed_and_command_exit_is_actual(self):
        process = self.fake_process()
        self.waitid.return_value = SimpleNamespace(si_pid=424242, si_code=guest.os.CLD_EXITED, si_status=97)
        secret = b'SYNTHETIC-SECRET-DO-NOT-ECHO'
        with patch.object(guest.subprocess, 'Popen', return_value=process), patch.object(guest, 'stop'), \
             patch.object(guest.select, 'select', return_value=([99], [], [])), \
             patch.object(guest.os, 'read', side_effect=[secret, b'']), patch.object(guest.sys, 'stdout') as stdout:
            code, data = guest.command(['/fixed/fake'], env={})
            self.assertEqual((code, data), (97, secret))
            stdout.write.assert_not_called()
            stdout.buffer.write.assert_not_called()
            self.assertNotIn('SECRET', json.dumps(guest.COMMAND_FACTS))
            self.assertEqual(guest.COMMAND_FACTS[0]['exitCode'], 97)


class CommandOwnershipTests(Isolated):
    def boundaries(self, *, terminal=False, merge=False, observations=None, cleanup_error=None):
        self.events = []
        self.state = {'reaped': False, 'descendantAlive': True}
        self.cleanup_error = cleanup_error
        def cleanup(name):
            self.events.append(name)
            if name == cleanup_error:
                raise OSError(errno.EPERM, name)
        def close(name):
            self.assertIn('reap', self.events)
            cleanup(name)
        def reap():
            self.assertIn('stop', self.events)
            self.assertFalse(self.state['reaped'])
            cleanup('reap')
            self.state['reaped'] = True
            return 0
        def killpg(pid, sig):
            self.assertEqual((pid, sig), (424242, guest.signal.SIGKILL))
            self.assertFalse(self.state['reaped'], 'signal after reap')
            cleanup('stop')
            self.state['descendantAlive'] = False
        owner = self
        class FakePopen:
            pid = 424242
            returncode = None
            stdout = SimpleNamespace(fileno=lambda: 99, close=lambda: close('stdout-close'))
            stderr = SimpleNamespace(fileno=lambda: 100, close=lambda: close('stderr-close'))
            def poll(self):
                raise AssertionError('poll reaps the reserved leader')
            def wait(self, *, timeout):
                owner.assertEqual(timeout, 3)
                self.returncode = reap()
                return self.returncode
        process = FakePopen()
        self.popen = self.enterContext(patch.object(guest.subprocess, 'Popen', return_value=process))
        self.fork = self.enterContext(patch.object(guest.pty, 'fork', return_value=(424242, 99)))
        self.enterContext(patch.object(guest.os, 'killpg', side_effect=killpg))
        def waitpid(pid, flags):
            self.assertEqual((pid, flags), (424242, 0), 'only cleanup may reap')
            return pid, reap()
        self.reap = self.enterContext(patch.object(guest.os, 'waitpid', side_effect=waitpid))
        self.enterContext(patch.object(guest.os, 'close', side_effect=lambda fd: (
            self.assertEqual(fd, 99), close('master-close'))))
        pending = iter(observations if observations is not None else [
            SimpleNamespace(si_pid=424242, si_code=guest.os.CLD_EXITED, si_status=0)])
        def observe(kind, pid, flags):
            self.assertEqual((kind, pid, flags), (1, 424242, 4 | 1 | 0x1000000))
            self.assertFalse(self.state['reaped'])
            self.events.append('observe')
            result = next(pending)
            if isinstance(result, BaseException):
                raise result
            return result
        self.waitid.side_effect = observe
        self.enterContext(patch.object(guest.select, 'select', side_effect=lambda fds, *a: (list(fds), [], [])))
        self.chunks = {99: iter([b'out', b'']), 100: iter([b'err', b''])}
        self.read = self.enterContext(patch.object(guest.os, 'read', side_effect=lambda fd, size: next(self.chunks[fd])))
        self.options = {'env': {}, 'terminal': terminal, 'merge': merge}

    def assert_cleanup(self, terminal):
        closes = ['master-close'] if terminal else ['stdout-close', 'stderr-close']
        self.assertEqual([event for event in self.events if event != 'observe'], ['stop', 'reap', *closes])
        self.assertTrue(self.state['reaped'])
        self.assertFalse(self.state['descendantAlive'])
        self.assertEqual(self.events.count('stop'), 1)
        self.assertLess(self.events.index('stop'), self.events.index('reap'))

    def test_exits_and_signals_keep_leader_until_group_stop_then_reap_and_all_closes(self):
        for terminal in (False, True):
            for kind, status, code in ((1, 0, 0), (1, 97, 97), (2, 15, -15), (3, 11, -11)):
                with self.subTest(terminal=terminal, kind=kind, status=status), ExitStack() as stack:
                    previous, self.stack = self.stack, stack
                    try:
                        self.boundaries(terminal=terminal, observations=[
                            SimpleNamespace(si_pid=424242, si_code=kind, si_status=status)])
                        self.assertEqual(guest.command(['/fixed/fake'], **self.options), (code, b'out'))
                        self.assert_cleanup(terminal)
                        self.assertEqual(guest.COMMAND_FACTS[-1]['exitCode'], code)
                        self.assertEqual(self.waitid.call_args.args, (1, 424242, 4 | 1 | 0x1000000))
                    finally:
                        self.stack = previous

    def test_closed_stdio_descendant_is_stopped_after_none_then_exit_observation(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal), ExitStack() as stack:
                previous, self.stack = self.stack, stack
                try:
                    self.boundaries(terminal=terminal, observations=[None, None,
                        SimpleNamespace(si_pid=424242, si_code=1, si_status=0)])
                    self.chunks = {99: iter([b'']), 100: iter([b''])}
                    self.assertEqual(guest.command(['/fixed/fake'], **self.options), (0, b''))
                    self.assertEqual(self.events[:3], ['observe'] * 3)
                    self.assert_cleanup(terminal)
                finally:
                    self.stack = previous

    def test_observed_exit_still_drains_output_and_times_out_on_open_descendant_stdio(self):
        self.boundaries()
        self.chunks = {99: iter([b'first', b'second', b'']), 100: iter([b'',])}
        self.assertEqual(guest.command(['/fixed/fake'], **self.options), (0, b'firstsecond'))
        self.assert_cleanup(False)
        self.assertEqual(self.events.count('observe'), 1)
        self.boundaries()
        error = guest.GuestFailure('command-timeout')
        with patch.object(guest, 'tick', side_effect=[None, None, error]):
            with self.assertRaises(guest.GuestFailure) as caught:
                guest.command(['/fixed/fake'], **self.options)
        self.assertIs(caught.exception, error)
        self.assert_cleanup(False)

    def test_timeout_cancel_feed_and_output_failure_preserve_first_error_and_cleanup(self):
        for terminal in (False, True):
            for reason in ('timeout', 'cancel', 'feed', 'output'):
                with self.subTest(terminal=terminal, reason=reason), ExitStack() as stack:
                    previous, self.stack = self.stack, stack
                    try:
                        self.boundaries(terminal=terminal)
                        error = KeyboardInterrupt() if reason == 'cancel' else guest.GuestFailure('first-error')
                        dialogue = None
                        if reason in ('timeout', 'cancel'):
                            self.enterContext(patch.object(guest, 'tick', side_effect=[None, error]))
                        elif reason == 'feed':
                            dialogue = SimpleNamespace(feed=Mock(side_effect=error))
                        else:
                            self.enterContext(patch.object(guest, 'MAX_OUTPUT', 1))
                        with self.assertRaises((guest.GuestFailure, KeyboardInterrupt)) as caught:
                            guest.command(['/fixed/fake'], dialogue=dialogue, **self.options)
                        if reason == 'output':
                            self.assertEqual(str(caught.exception), 'command-output-limit')
                        else:
                            self.assertIs(caught.exception, error)
                        self.assert_cleanup(terminal)
                    finally:
                        self.stack = previous

    def test_cleanup_failures_attempt_reap_and_every_close_without_masking_feed_error(self):
        for terminal in (False, True):
            for boundary in ('stop', 'reap', 'master-close' if terminal else 'stdout-close', 'stderr-close'):
                if terminal and boundary == 'stderr-close':
                    continue
                with self.subTest(terminal=terminal, boundary=boundary), ExitStack() as stack:
                    previous, self.stack = self.stack, stack
                    try:
                        self.boundaries(terminal=terminal, cleanup_error=boundary)
                        error = guest.GuestFailure('first-error')
                        with self.assertRaises(guest.GuestFailure) as caught:
                            guest.command(['/fixed/fake'], dialogue=SimpleNamespace(feed=Mock(side_effect=error)),
                                          **self.options)
                        self.assertIs(caught.exception, error)
                        expected = ['stop', 'reap'] + (['master-close'] if terminal else ['stdout-close', 'stderr-close'])
                        self.assertEqual(self.events, expected)
                        self.assertEqual(self.state['reaped'], boundary != 'reap')
                    finally:
                        self.stack = previous

    def test_cleanup_error_after_observed_success_propagates_after_all_attempts(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal), ExitStack() as stack:
                previous, self.stack = self.stack, stack
                try:
                    self.boundaries(terminal=terminal, cleanup_error='stop')
                    with self.assertRaisesRegex(OSError, 'stop'):
                        guest.command(['/fixed/fake'], **self.options)
                    self.assertEqual(self.events, ['observe', 'stop', 'reap'] + (
                        ['master-close'] if terminal else ['stdout-close', 'stderr-close']))
                finally:
                    self.stack = previous

    def test_unsupported_waitid_and_unknown_status_fail_closed_without_poll_fallback(self):
        for terminal in (False, True):
            for observation in (OSError(errno.EINVAL, 'unsupported flags'), NotImplementedError('waitid'),
                                SimpleNamespace(si_pid=424242, si_code=99, si_status=0),
                                SimpleNamespace(si_pid=424242, si_code=True, si_status=0),
                                SimpleNamespace(si_pid=424243, si_code=1, si_status=0),
                                SimpleNamespace(si_pid=424242, si_code=1, si_status=-1),
                                SimpleNamespace(si_pid=424242, si_code=2, si_status=0)):
                with self.subTest(terminal=terminal, observation=observation), ExitStack() as stack:
                    previous, self.stack = self.stack, stack
                    try:
                        self.boundaries(terminal=terminal, observations=[observation])
                        with self.assertRaises((guest.GuestFailure, OSError, NotImplementedError)):
                            guest.command(['/fixed/fake'], **self.options)
                        self.assert_cleanup(terminal)
                    finally:
                        self.stack = previous

    def test_already_reaped_child_revokes_signal_authority_and_still_closes_all_fds(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal), ExitStack() as stack:
                previous, self.stack = self.stack, stack
                try:
                    self.boundaries(terminal=terminal)
                    error = ChildProcessError(errno.ECHILD, 'already reaped')
                    def observed_reaped(*args):
                        self.state['reaped'] = True
                        raise error
                    self.waitid.side_effect = observed_reaped
                    def reap_again(*args, **kwargs):
                        self.events.append('reap')
                        raise ChildProcessError(errno.ECHILD, 'still absent')
                    if terminal:
                        self.reap.side_effect = reap_again
                    else:
                        self.popen.return_value.wait = Mock(side_effect=reap_again)
                    with self.assertRaises(ChildProcessError) as caught:
                        guest.command(['/fixed/fake'], **self.options)
                    self.assertIs(caught.exception, error)
                    self.assertEqual(self.events, ['reap'] + (
                        ['master-close'] if terminal else ['stdout-close', 'stderr-close']))
                finally:
                    self.stack = previous

    def test_missing_capabilities_refuse_before_either_spawn(self):
        self.boundaries()
        for name in ('waitid', 'P_PID', 'WEXITED', 'WNOHANG', 'WNOWAIT', 'CLD_EXITED', 'CLD_KILLED', 'CLD_DUMPED'):
            for terminal in (False, True):
                with self.subTest(name=name, terminal=terminal), patch.object(guest.os, name, None):
                    with self.assertRaisesRegex(guest.GuestFailure, '^command-failed$'):
                        guest.command(['/fixed/fake'], env={}, terminal=terminal)
        self.popen.assert_not_called()
        self.fork.assert_not_called()
        self.assertEqual(self.events, [])


class LifecycleTests(Isolated):
    def setUp(self):
        super().setUp()
        fixture = receipt_fixtures.ReceiptTests()
        fixture.setUp()
        self.row = fixture.row
        self.entries = {}
        for name in guest.STAGES[1:]:
            stage = self.row['stages'][name]
            s = stage['journal']
            self.entries[name] = {'operationId': s['operationId'], 'operation': 'apply',
                'result': s['result'], 'terminal': s['terminal'], 'journalPersisted': s['journalPersisted'],
                'source': {'verified': s['sourceVerified'], 'frozenPath': s['sourceFrozenPath'],
                           'outputPath': s['sourceOutputPath']}, 'binding': {'target': s['bindingTarget']} if s['bindingTarget'] else None,
                'stages': [{'name': 'activate', 'status': s['activateStatus']}]
                          + ([{'name': 'verify', 'status': s['verifyStatus']}] if s['verifyStatus'] else []),
                'conflict': s['conflictPartial'], 'exitCode': 97 if name == 'faultApply' else 0}
        self.row['stages']['faultApply']['exitCode'] = 97

    def lifecycle_fakes(self, *, fail=None, cancel=False, old_missing=False):
        self.calls = []
        self.enterContext(patch.object(guest, 'setup_stage', side_effect=lambda *a: self.row['stages']['setup']))
        self.enterContext(patch.object(guest, 'tui_tests', return_value=True))
        self.enterContext(patch.object(guest, 'private_read', return_value=b'fixed original machine'))
        self.enterContext(patch.object(guest, 'write_new'))
        self.enterContext(patch.object(Path, 'write_text'))
        self.enterContext(patch.object(Path, 'write_bytes'))
        self.enterContext(patch.object(Path, 'unlink'))
        def apply(env, *, fault=False, tui=False):
            name = ('firstApply', 'repeatApply', 'faultApply', 'recoveryApply')[len(self.calls)]
            self.calls.append(name)
            self.assertEqual(fault, name == 'faultApply')
            if name == fail:
                raise KeyboardInterrupt if cancel else guest.GuestFailure('fixed-failure')
            row = copy.deepcopy(self.row['stages'][name])
            if fault:
                row['originalMachineRestored'] = row['partialReadableAfterRecovery'] = False
            return row
        self.enterContext(patch.object(guest, 'apply_stage', side_effect=apply))
        journal = Mock()
        journal.read.return_value = [] if old_missing else [self.entries['faultApply'], self.entries['recoveryApply']]
        self.enterContext(patch.object(guest, 'backend', return_value=(journal, Mock())))

    def test_fixed_stage_order_success_receipt_satisfies_existing_acceptance_contract(self):
        self.lifecycle_fakes()
        stages, error = guest.lifecycle({}, b'synthetic-revision')
        self.assertIsNone(error)
        self.assertEqual(self.calls, list(guest.STAGES[1:]))
        row = {**self.row, 'stages': stages}
        raw = (guest.INSTALL_PREFIX + json.dumps(row['install']) + '\n' + guest.PREFIX + json.dumps(row) + '\n').encode()
        self.assertEqual(acceptance.receipt_from(raw, TOKEN, row['source']), row)

    def test_each_apply_failure_stops_later_mutations_and_keeps_completed_facts(self):
        for failed in guest.STAGES[1:]:
            with self.subTest(failed=failed):
                with patch.object(guest, 'setup_stage', return_value=self.row['stages']['setup']), \
                     patch.object(guest, 'tui_tests', return_value=True), \
                     patch.object(guest, 'private_read', return_value=b'baseline'), \
                     patch.object(guest, 'write_new'), patch.object(Path, 'write_text'), \
                     patch.object(Path, 'write_bytes') as restore, patch.object(Path, 'unlink'), \
                     patch.object(guest, 'apply_stage') as apply:
                    names = list(guest.STAGES[1:])
                    apply.side_effect = [copy.deepcopy(self.row['stages'][n]) if n != failed else guest.GuestFailure('fail')
                                         for n in names]
                    stages, error = guest.lifecycle({}, b'rev')
                    self.assertEqual(apply.call_count, names.index(failed) + 1)
                    self.assertEqual(error, 'failed-or-unknown:' + failed)
                    self.assertEqual(set(stages), {'setup', *names[:names.index(failed)]})
                    if failed != 'recoveryApply':
                        restore.assert_not_called()

    def test_timeout_and_cancel_preserve_old_partial_and_do_not_claim_rollback(self):
        self.lifecycle_fakes(fail='recoveryApply', cancel=True)
        stages, error = guest.lifecycle({}, b'rev')
        self.assertEqual(error, 'cancelled')
        self.assertEqual(stages['faultApply']['journal']['terminal'], 'partial')
        self.assertTrue(stages['faultApply']['originalMachineRestored'])
        self.assertFalse(stages['faultApply']['partialReadableAfterRecovery'])
        self.assertNotIn('recoveryApply', stages)
        self.assertNotIn('rollback', json.dumps(stages).lower())

    def test_old_partial_must_be_read_back_after_recovery_not_inferred_from_new_generation(self):
        self.lifecycle_fakes(old_missing=True)
        stages, error = guest.lifecycle({}, b'rev')
        self.assertEqual(error, 'failed-or-unknown:recoveryApply')
        self.assertFalse(stages['faultApply']['partialReadableAfterRecovery'])

    def test_tui_evidence_requires_docheck_and_actual_guest_rebuild(self):
        drv = store('tui.drv')
        calls = []
        def nix(args, env, **kwargs):
            calls.append(args)
            if args[0] == 'eval':
                return drv.encode()
            if args[0] == 'derivation':
                return json.dumps({drv: {'env': {'doCheck': '1'}}}).encode()
            return b'[]'
        with patch.object(guest, 'nix', side_effect=nix):
            self.assertIs(guest.tui_tests({}), True)
        self.assertEqual(len(calls), 4)
        self.assertIn('--rebuild', calls[-1])
        with patch.object(guest, 'nix', side_effect=[drv.encode(), json.dumps({drv: {'env': {}}}).encode()]):
            with self.assertRaisesRegex(guest.GuestFailure, 'tui-check-disabled'):
                guest.tui_tests({})

    def test_fault_apply_collects_real_partial_rejects_retry_and_requires_exit97_marker(self):
        journal = Mock()
        journal.read.side_effect = [[], [self.entries['faultApply']]]
        journal.retry_policy.return_value = {'allowed': False, 'step': 'verify-generation'}
        journal.verify_retry.side_effect = ValueError('fixed refusal')
        generation = Mock(return_value=None)
        with patch.object(guest, 'backend', return_value=(journal, generation)), \
             patch.object(guest, 'command', return_value=(97, b'ENVY_RUNTIME_FIXED_FAULT_97\n')):
            stage = guest.apply_stage({}, fault=True)
        self.assertEqual(stage['exitCode'], 97)
        self.assertFalse(stage['retryAllowed'])
        self.assertIsNone(stage['observedGeneration'])
        journal.verify_retry.assert_called_once_with(self.entries['faultApply']['operationId'], 'verify-generation')
        for code, output in ((0, b'ENVY_RUNTIME_FIXED_FAULT_97'), (97, b'build failed'), (1, b'ENVY_RUNTIME_FIXED_FAULT_97')):
            journal.read.side_effect = [[], [{**self.entries['faultApply'], 'exitCode': code}]]
            with patch.object(guest, 'backend', return_value=(journal, generation)), \
                 patch.object(guest, 'command', return_value=(code, output)), self.assertRaises(guest.GuestFailure):
                guest.apply_stage({}, fault=True)

    def test_failed_apply_keeps_actual_exit_output_digest_and_unconfirmed_journal(self):
        journal = Mock()
        entry = {**self.entries['firstApply'], 'journalPersisted': False, 'exitCode': 1}
        journal.read.side_effect = [[], [entry]]
        with patch.object(guest, 'backend', return_value=(journal, Mock())), \
             patch.object(guest, 'command', return_value=(1, b'SYNTHETIC-RAW-SECRET')):
            with self.assertRaises(guest.GuestFailure) as caught:
                guest.apply_stage({})
        observation = caught.exception.observation
        self.assertEqual(observation['exitCode'], 1)
        self.assertFalse(observation['journal']['journalPersisted'])
        self.assertEqual(observation['status'], 'partial-or-unknown')
        self.assertNotIn('RAW-SECRET', json.dumps(observation))
        with patch.object(guest, 'setup_stage', return_value=self.row['stages']['setup']), \
             patch.object(guest, 'tui_tests', return_value=True), \
             patch.object(guest, 'apply_stage', side_effect=caught.exception) as apply:
            stages, error = guest.lifecycle({}, b'rev')
        self.assertEqual(stages['firstApply'], observation)
        self.assertEqual(error, 'failed-or-unknown:firstApply')
        self.assertEqual(apply.call_count, 1)

    def test_durable_journal_requires_terminal_confirmation_and_source_binding(self):
        for name in guest.STAGES[1:]:
            entry = self.entries[name]
            self.assertEqual(guest.validate_journal(entry, fault=name == 'faultApply'),
                             self.row['stages'][name]['journal'])
            for key, value in (('journalPersisted', False), ('terminal', None), ('result', 'interrupted')):
                bad = {**entry, key: value}
                with self.assertRaises(guest.GuestFailure):
                    guest.validate_journal(bad, fault=name == 'faultApply')

    def test_secret_facts_read_metadata_only_and_never_open_secret_values(self):
        regular = SimpleNamespace(st_mode=0o100600)
        with patch.object(guest.os, 'environ', {'XDG_RUNTIME_DIR': '/run/user/1000'}), \
             patch.object(Path, 'stat', return_value=regular), \
             patch.object(guest.os, 'open', side_effect=AssertionError('secret read forbidden')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('secret read forbidden')):
            facts = guest.secret_facts()
        self.assertEqual(set(facts), {'ageKeyPresent', 'ageKeyMode0600', 'decrypted'})
        self.assertEqual(set(facts['decrypted']), set(guest.SECRET_NAMES))
        self.assertNotIn('PRIVATE', json.dumps(facts))


class GuestFetchTests(Isolated):
    def setUp(self):
        super().setUp()
        self.meta = network_metadata()
        self.env = guest.environment(transport='guest-fetch', lock_sha256=self.meta['source']['lockSha256'])
        self.tools = {name: store(name) for name in ('gitMinimal', 'sops', 'age', 'ssh-to-age')}

    def test_closed_network_metadata_and_mode_binding_leave_legacy_shape_intact(self):
        self.assertEqual(guest.validate_metadata(self.meta, 'guest-fetch'), {store('source')})
        self.assertEqual(set(self.meta), {'schemaVersion', 'transport', 'source', 'snapshot', 'sourceNarSize'})
        program = guest.guest_program(TOKEN, self.meta)
        scope = {'__name__': 'inert_network_guest'}
        exec(compile(program, '<network program>', 'exec'), scope)
        self.assertEqual(scope['RUN_CONFIG'], network_config())
        self.assertEqual(guest.guest_program(TOKEN, metadata()).splitlines()[0].count("'nix-export'"), 1)
        for row, mode in ((self.meta, 'nix-export'), (metadata(), 'guest-fetch')):
            with self.subTest(mode=mode), self.assertRaisesRegex(guest.GuestFailure, 'metadata-transport'):
                guest.validate_metadata(row, mode)
        for field in ('preparation', 'objects', 'export', 'network', 'sourcePath'):
            bad = {**self.meta, field: {}}
            with self.subTest(field=field), self.assertRaises(guest.GuestFailure):
                guest.guest_program(TOKEN, bad)
        for size in (0, -1, True, 1.5, guest.MAX_SOURCE * 2 + 1):
            with self.subTest(size=size), self.assertRaises(guest.GuestFailure):
                guest.validate_metadata({**self.meta, 'sourceNarSize': size})
        for mode in ('nix-export', 'unknown', None):
            with self.subTest(mode=mode), self.assertRaises(guest.GuestFailure):
                guest.validate_metadata({**self.meta, 'transport': mode})
        with self.assertRaises(guest.GuestFailure):
            guest.validate_config({**network_config(), 'transport': 'unknown'})
        for key in ('preparation', 'transport', 'sourceNarSize'):
            bad = metadata()
            bad[key] = self.meta.get(key)
            with self.subTest(old_key=key), self.assertRaises(guest.GuestFailure):
                guest.validate_metadata(bad)
        for field in ('narSize', 'hostPath', 'transport'):
            bad = copy.deepcopy(self.meta)
            bad['source'][field] = 'extra'
            with self.assertRaises(guest.GuestFailure):
                guest.validate_metadata(bad)

    def test_network_guard_waits_only_for_exact_device_and_rejects_other_nics_and_macs(self):
        lo, nic, other = Path('lo'), Path('envy0'), Path('eth0')
        with patch.object(Path, 'iterdir', side_effect=[[lo], [lo, nic]]), \
             patch.object(Path, 'read_text', return_value=guest.NETWORK_MAC + '\n'), \
             patch.object(guest.time, 'sleep') as wait:
            guest.network_guard('guest-fetch')
        wait.assert_called_once_with(0.1)
        for names, mac in (([lo, other], guest.NETWORK_MAC), ([lo, nic, other], guest.NETWORK_MAC),
                           ([lo, nic], '52:54:00:00:00:00'), ([lo, nic], guest.NETWORK_MAC.upper())):
            with self.subTest(names=names, mac=mac), patch.object(Path, 'iterdir', return_value=names), \
                 patch.object(Path, 'read_text', return_value=mac), self.assertRaises(guest.GuestFailure):
                guest.network_guard('guest-fetch')
        with patch.object(Path, 'iterdir', return_value=[lo]), \
             patch.object(guest.time, 'monotonic', side_effect=[1, 1, 32]), self.assertRaisesRegex(guest.GuestFailure, 'guest-network-timeout'):
            guest.network_guard('guest-fetch')
        with patch.object(Path, 'iterdir', return_value=[lo, nic]), self.assertRaises(guest.GuestFailure):
            guest.network_guard('nix-export')
        with patch.object(Path, 'iterdir', return_value=[lo]):
            guest.network_guard('nix-export')

    def test_guard_routes_validated_mode_to_network_check(self):
        for config in (CONFIG, network_config()):
            with patch.object(guest.sys, 'platform', 'linux'), patch.object(guest.platform, 'machine', return_value='x86_64'), \
                 patch.object(guest.os, 'getuid', return_value=0), patch.object(guest.os, 'geteuid', return_value=0), \
                 patch.object(guest, 'private_read', return_value=(TOKEN + '\n').encode()), \
                 patch.object(guest.os, 'stat', side_effect=[
                     SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=ROOT_DEVICE),
                     SimpleNamespace(st_mode=stat.S_IFDIR, st_dev=ROOT_DEVICE)]), \
                 patch.object(Path, 'read_text', return_value='mount inventory'), patch.object(guest, 'mounts'), \
                 patch.object(guest, 'network_guard') as network:
                guest.guard(config, root=True)
            network.assert_called_once_with(config['transport'])

    def test_environment_and_all_nix_commands_allow_native_network_without_host_settings(self):
        inherited = {'https_proxy': 'SYNTHETIC-HOST-PROXY', 'NIX_CONFIG': 'ssl-verify = false',
                     'AWS_SECRET_ACCESS_KEY': 'SYNTHETIC-PRIVATE', 'SSH_AUTH_SOCK': '/host/agent',
                     'ENVY_DEV_SHELL': '/host/shell'}
        with patch.dict(guest.os.environ, inherited, clear=True):
            for user in (True, False):
                env = guest.environment(transport='guest-fetch', tools=self.tools, user=user)
                self.assertFalse(set(inherited) - {'NIX_CONFIG'} & set(env))
                self.assertEqual(env['NIX_CONFIG'], guest.nix_config('guest-fetch'))
                self.assertIn('require-sigs = true', env['NIX_CONFIG'])
                self.assertIn('ssl-verify = true', env['NIX_CONFIG'])
                self.assertIn('substituters = https://cache.nixos.org', env['NIX_CONFIG'])
                self.assertIn('builders =\n', env['NIX_CONFIG'])
                with patch.object(guest, 'checked', return_value=b'{}') as checked:
                    guest.nix(['build', '--no-update-lock-file', 'fixed'], env)
                argv = checked.call_args.args[0]
                self.assertNotIn('--offline', argv)
                self.assertNotIn('require-sigs', argv)
                self.assertNotIn('substituters', argv)
                self.assertIs(checked.call_args.kwargs['env'], env)
        with patch.object(guest, 'checked', return_value=b'{}') as checked:
            guest.nix(['eval', 'fixed'], guest.environment())
        self.assertIn('--offline', checked.call_args.args[0])
        for env in ({**self.env, 'ENVY_RUNTIME_TRANSPORT': 'unknown'},
                    {**self.env, 'NIX_CONFIG': guest.nix_config('nix-export')},
                    {**guest.environment(), 'NIX_CONFIG': guest.nix_config('guest-fetch')}):
            with patch.object(guest, 'checked') as checked, self.assertRaises(guest.GuestFailure):
                guest.nix(['eval', 'fixed'], env)
            checked.assert_not_called()

    def test_source_native_binding_precedes_first_repository_evaluation(self):
        calls = []
        actual = {'path': store('source'), 'narHash': self.meta['source']['narHash'], 'narSize': 100, 'references': []}
        def nix(args, env, **kwargs):
            calls.append(args)
            if args[:2] == ['store', 'add-path']:
                return (store('source') + '\n').encode()
            if args[0] == 'path-info':
                return json.dumps([actual]).encode()
            return json.dumps({'path': store('source'), 'inputs': {'nixpkgs': {'path': store('nixpkgs')}}}).encode()
        with patch.object(guest, 'nix', side_effect=nix), patch.object(guest, 'verify_lock') as lock, \
             patch.object(guest, 'bootstrap_tools', return_value=self.tools) as tools:
            self.assertEqual(guest.fetch_payload(self.meta, self.env, Path('/guest/new-source'), network_config(self.meta)), self.tools)
        self.assertEqual(calls[0], ['store', 'add-path', '--name', 'source', '/guest/new-source'])
        self.assertEqual(calls[1], ['path-info', '--json', store('source')])
        self.assertEqual(calls[2], ['flake', 'archive', '--json', '--no-update-lock-file', 'path:' + store('source')])
        tools.assert_called_once_with(self.meta, self.env)
        self.assertEqual(lock.call_count, 4)
        for change in ('path', 'narHash', 'narSize'):
            bad = {**actual, change: store('wrong') if change == 'path' else sri(b'wrong') if change == 'narHash' else 101}
            responses = [(store('wrong') + '\n').encode()] if change == 'path' else [
                (store('source') + '\n').encode(), json.dumps([bad]).encode()]
            with self.subTest(change=change), patch.object(guest, 'nix', side_effect=responses) as native, \
                 patch.object(guest, 'bootstrap_tools') as tools, self.assertRaises(guest.GuestFailure):
                guest.fetch_payload(self.meta, self.env, Path('/guest/new-source'), network_config(self.meta))
            tools.assert_not_called()
            self.assertTrue(all(c.args[0][0] in ('store', 'path-info') for c in native.call_args_list))

    def test_bootstrap_builds_production_setup_then_real_tools_before_eval_and_executable_checks(self):
        self.meta['source']['narHash'] = 'sha256-' + ('+/v7' * 10) + '+/s='
        events = []
        rows = [{'outputs': {'out': path}} for path in self.tools.values()]
        def nix(args, env, **kwargs):
            events.append(args)
            if args[0] == 'eval':
                return json.dumps(self.tools).encode()
            if '--json' in args:
                return json.dumps(rows).encode()
            return b''
        def check(tools):
            events.append('check-executables')
            self.assertEqual(tools, self.tools)
        with patch.object(guest, 'nix', side_effect=nix), patch.object(guest, 'verify_tools', side_effect=check):
            self.assertEqual(guest.bootstrap_tools(self.meta, self.env), self.tools)
        self.assertEqual(events[0], ['build', '--no-link', '--no-update-lock-file', 'path:' + store('source') + '#setup'])
        self.assertEqual([row[0] for row in events[:3]], ['build', 'build', 'eval'])
        self.assertIn('builtins.attrValues', events[1][-1])
        self.assertIn('f.inputs.nixpkgs.legacyPackages.x86_64-linux', events[1][-1])
        for row in events[1:3]:
            ref, _ = json.JSONDecoder().raw_decode(row[-1].split('builtins.getFlake ', 1)[1])
            parsed = urlsplit(ref)
            self.assertEqual(parsed.scheme, 'path')
            self.assertEqual(parsed.path, self.meta['source']['path'])
            self.assertEqual(parse_qs(parsed.query), {'narHash': [self.meta['source']['narHash']]})
            self.assertNotIn('--impure', row)
        self.assertTrue(all('--no-update-lock-file' in row for row in events[:3]))
        self.assertEqual(events[-1], 'check-executables')
        for bad in ([], rows[:3], [{**row, 'outputs': {'out': store('missing')}} for row in rows]):
            with patch.object(guest, 'nix', side_effect=[b'', json.dumps(bad).encode(), json.dumps(self.tools).encode()]), \
                 patch.object(guest, 'verify_tools') as check, self.assertRaises(guest.GuestFailure):
                guest.bootstrap_tools(self.meta, self.env)
            check.assert_not_called()
        for files, executable in ((False, True), (True, False)):
            with patch.object(Path, 'is_file', return_value=files), patch.object(guest.os, 'access', return_value=executable), \
                 self.assertRaisesRegex(guest.GuestFailure, 'bootstrap-not-executable'):
                guest.verify_tools(self.tools)
        with patch.object(Path, 'is_file', return_value=True), patch.object(guest.os, 'access', return_value=True) as access:
            guest.verify_tools(self.tools)
        self.assertEqual([str(c.args[0]) for c in access.call_args_list], [
            self.tools[name] + '/bin/' + binary for name, binary in
            (('gitMinimal', 'git'), ('sops', 'sops'), ('age', 'age'), ('ssh-to-age', 'ssh-to-age'))])

    def test_archive_and_bootstrap_lock_checks_run_even_when_network_or_build_fails(self):
        actual = {'path': store('source'), 'narHash': self.meta['source']['narHash'], 'narSize': 100, 'references': []}
        prefix = [(store('source') + '\n').encode(), json.dumps([actual]).encode()]
        for phase in ('archive', 'bootstrap'):
            responses = prefix + ([guest.GuestFailure('network-unavailable')] if phase == 'archive' else [
                json.dumps({'path': store('source')}).encode()])
            with self.subTest(phase=phase), patch.object(guest, 'nix', side_effect=responses), \
                 patch.object(guest, 'bootstrap_tools', side_effect=guest.GuestFailure('build-failed')) as tools, \
                 patch.object(guest, 'verify_lock') as lock, self.assertRaises(guest.GuestFailure):
                guest.fetch_payload(self.meta, self.env, Path('/guest/new-source'), network_config(self.meta))
            self.assertEqual(lock.call_count, 2 if phase == 'archive' else 4)
            if phase == 'archive':
                tools.assert_not_called()
        with patch.object(guest, 'private_read', return_value=b'changed lock'), self.assertRaisesRegex(guest.GuestFailure, 'lock-drift') as caught:
            guest.verify_lock(self.env)
        self.assertEqual(caught.exception.observation, {'status': 'partial-or-unknown'})

    def test_setup_production_subprocess_gets_network_environment_and_checks_lock_on_failure(self):
        for result in ((1, b''), KeyboardInterrupt()):
            with patch.object(guest, 'command', side_effect=[result]) as command, \
                 patch.object(guest, 'verify_lock') as lock, self.assertRaises((guest.GuestFailure, KeyboardInterrupt)):
                guest.setup_stage(self.env, b'fixed')
            self.assertIsNot(command.call_args.kwargs['env'], self.env)
            self.assertEqual(command.call_args.kwargs['env'], {**self.env, 'ENVY_DEBUG': '1'})
            self.assertNotIn('ENVY_DEBUG', self.env)
            self.assertEqual(command.call_args.args[0][0:2], ['/bin/bash', str(guest.REPO / 'install.sh')])
            lock.assert_called_once_with(self.env)

    def test_every_apply_uses_network_config_and_lock_drift_preserves_partial_observations(self):
        for name in guest.STAGES[1:]:
            journal = Mock()
            journal.read.return_value = []
            with self.subTest(stage=name), patch.object(guest, 'backend', return_value=(journal, Mock())), \
                 patch.object(guest, 'command', return_value=(97 if name == 'faultApply' else 0, b'actual-output')) as command, \
                 patch.object(guest, 'private_read', return_value=b'changed'), self.assertRaisesRegex(guest.GuestFailure, 'lock-drift') as caught:
                guest.apply_stage(self.env, fault=name == 'faultApply')
            self.assertNotIn('--offline', command.call_args.args[0])
            self.assertIn('--no-update-lock-file', command.call_args.args[0])
            self.assertIs(command.call_args.kwargs['env'], self.env)
            self.assertEqual(caught.exception.observation['status'], 'partial-or-unknown')
            self.assertEqual(caught.exception.observation['outputSha256'], guest.sha(b'actual-output'))
            self.assertEqual(journal.read.call_count, 1)

    def test_all_lifecycle_phases_stop_on_lock_drift_with_partial_status(self):
        fixture = LifecycleTests()
        fixture.setUp()
        self.addCleanup(fixture.stack.close)
        for phase in guest.STAGES:
            failure = guest.GuestFailure('lock-drift')
            failure.observation = {'status': 'partial-or-unknown'}
            names = list(guest.STAGES[1:])
            with self.subTest(phase=phase), patch.object(guest, 'setup_stage',
                     side_effect=failure if phase == 'setup' else None, return_value=fixture.row['stages']['setup']), \
                 patch.object(guest, 'tui_tests', return_value=True), patch.object(guest, 'private_read', return_value=b'baseline'), \
                 patch.object(guest, 'write_new'), patch.object(Path, 'write_text'), patch.object(Path, 'write_bytes'), \
                 patch.object(Path, 'unlink'), patch.object(guest, 'apply_stage', side_effect=[
                     failure if name == phase else copy.deepcopy(fixture.row['stages'][name]) for name in names]) as apply:
                stages, error = guest.lifecycle(self.env, b'rev')
            self.assertEqual(error, 'failed-or-unknown:' + phase)
            self.assertEqual(stages[phase], {'status': 'partial-or-unknown'})
            self.assertEqual(apply.call_count, 0 if phase == 'setup' else names.index(phase) + 1)

    def test_nix_indirect_production_builds_verify_lock_even_on_command_failure(self):
        for response in (b'{}', guest.GuestFailure('failed')):
            with patch.object(guest, 'checked', side_effect=[response]) as checked, \
                 patch.object(guest, 'verify_lock') as lock:
                if isinstance(response, Exception):
                    with self.assertRaises(guest.GuestFailure):
                        guest.nix(['build', '--no-update-lock-file', 'path:guest#setup'], self.env, cwd=str(guest.REPO))
                else:
                    guest.nix(['build', '--no-update-lock-file', 'path:guest#setup'], self.env, cwd=str(guest.REPO))
            lock.assert_called_once_with(self.env)
            self.assertNotIn('--offline', checked.call_args.args[0])

    def test_root_network_path_validates_extracts_installs_fetches_and_binds_readback(self):
        fixture = receipt_fixtures.ReceiptTests()
        fixture.setUp()
        row = {**copy.deepcopy(fixture.row), 'source': self.meta['source'], 'install': None,
               'transport': 'guest-fetch', 'evidence': 'linux-vm-envy-lifecycle-networked'}
        facts = {'token': TOKEN, 'commandCount': 0, 'lastCommands': []}
        events, native = [], []
        item = {'path': store('source'), 'narHash': self.meta['source']['narHash'], 'narSize': 100, 'references': []}
        def checked(argv, **kwargs):
            self.assertEqual(kwargs['env']['NIX_CONFIG'], guest.nix_config('guest-fetch'))
            if argv[0] == guest.NIX:
                self.assertNotIn('--offline', argv)
                self.assertNotIn('require-sigs', argv)
                self.assertNotIn('substituters', argv)
                native.append(argv)
                if 'config' in argv:
                    events.append('native-ca')
                    return (guest.SYSTEM_CA_BUNDLE + '\n').encode()
                if 'ping' in argv:
                    events.append('native-cache')
                    return b'{}'
                if 'add-path' in argv:
                    events.append('add-path')
                    return (store('source') + '\n').encode()
                if 'path-info' in argv:
                    events.append('native-info')
                    return json.dumps([item]).encode()
                if 'archive' in argv:
                    events.append('archive')
                    return json.dumps({'path': store('source'), 'inputs': {}}).encode()
                if 'build' in argv:
                    events.append('tools-build' if '--expr' in argv else 'setup-build')
                    if '--json' in argv:
                        return json.dumps([{'outputs': {'out': path}} for path in self.tools.values()]).encode()
                if 'eval' in argv:
                    events.append('resolve-tools')
                    return json.dumps(self.tools).encode()
                return b''
            events.append(argv[0])
            return b''
        for target, kwargs in ((guest.os.path, {'lexists': False}), (Path, {'mkdir': None, 'is_file': True}),
                               (guest.os, {'close': None, 'dup': 11, 'access': True})):
            for name, value in kwargs.items():
                self.enterContext(patch.object(target, name, return_value=value))
        def read(path, *args, **kwargs):
            if path == guest.RESULT:
                return json.dumps(row).encode()
            if path == guest.OBSERVATIONS:
                return json.dumps(facts).encode()
            self.assertEqual(path.name, 'flake.lock')
            return b'fixed-lock'
        self.meta['source']['lockSha256'] = guest.sha(b'fixed-lock')
        row['source'] = self.meta['source']
        def ca_stat(path, *, follow_symlinks):
            self.assertEqual((path, follow_symlinks), (guest.SYSTEM_CA_BUNDLE, False))
            self.assertEqual(events, ['guard'])
            events.append('ca-metadata')
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)
        def curl(argv, **kwargs):
            self.assertIn(events[-1], ('daemon-ca', 'tls-nix-cache', 'tls-github-api'))
            self.assertEqual(len(native), 0)
            events.append('tls-' + guest.PUBLIC_TLS_ENDPOINTS[len([e for e in events if e.startswith('tls-')])][0])
            return 0, b'0 404\n'
        def daemon_ca(env):
            self.assertEqual(self.serial.call_args.args[0], guest.INSTALL_PREFIX)
            events.append('daemon-ca')
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(guest, 'configure_daemon_ca', side_effect=daemon_ca))
        with patch.object(guest, 'guard', side_effect=lambda *a, **k: events.append('guard')), \
             patch.object(guest.os, 'stat', side_effect=ca_stat), patch.object(guest, 'command', side_effect=curl), \
             patch.object(guest.os, 'read', return_value=json.dumps(self.meta).encode()), \
             patch.object(guest, 'payload_fd', side_effect=[10, 11]) as payload, \
             patch.object(guest.os, 'fdopen', return_value=io.BytesIO(b'tar')), \
             patch.object(guest, 'source_members', side_effect=lambda *a: events.append('verify-tar') or {}) as members, \
             patch.object(guest, 'extract_source', side_effect=lambda *a: events.append('extract-new') or guest.HOME / 'source-fixture'), \
             patch.object(guest, 'install_official', side_effect=lambda *a: events.append('official') or fixture.installed), \
             patch.object(guest, 'import_payload', side_effect=AssertionError('network export import forbidden')), \
             patch.object(guest, 'write_new') as write, \
             patch.object(guest.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=1000, pw_gid=1000)), \
             patch.object(guest.os, 'walk', return_value=[]), patch.object(guest.os, 'chown'), \
             patch.object(guest, 'checked', side_effect=checked) as check, patch.object(guest, 'stop_user_unit'), \
             patch.object(guest, 'private_read', side_effect=read), \
             patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as stdout:
            result = guest.root_stages(network_config(self.meta))
        self.assertEqual(events[:19], ['guard', 'ca-metadata', '/usr/bin/mount', 'guard', 'verify-tar',
                                      'extract-new', 'official', 'daemon-ca', 'tls-nix-cache', 'tls-github-api',
                                      'tls-github-codeload', 'native-ca', 'native-cache', 'add-path', 'native-info', 'archive',
                                      'setup-build', 'tools-build', 'resolve-tools'])
        self.assertEqual(len(native), 8)
        self.assertEqual([call.args[0] for call in payload.call_args_list], ['acceptance.json', 'source.tar'])
        members.assert_called_once_with(b'tar', self.meta)
        self.prefetch.assert_called_once_with(self.meta, unittest.mock.ANY, guest.HOME / 'source-fixture',
                                              network_config(self.meta))
        self.assertEqual(result['transport'], 'guest-fetch')
        self.assertEqual(result['evidence'], 'linux-vm-envy-lifecycle-networked')
        self.assertEqual(result['install'], fixture.installed)
        self.assertEqual(set(result), set(fixture.row) | {'transport'})
        context = json.loads(write.call_args.args[1])
        self.assertEqual(context, {'metadata': self.meta, 'tools': self.tools})
        self.assertEqual(write.call_args.args[2], 0o444)
        argv = check.call_args.args[0]
        self.assertIn('--property=PAMName=login', argv)
        self.assertIn('--setenv=ENVY_RUNTIME_TRANSPORT=guest-fetch', argv)
        self.assertIn('--setenv=NIX_CONFIG=' + guest.nix_config('guest-fetch'), argv)
        self.assertNotIn('--offline', ' '.join(argv))
        self.assertEqual(stdout.getvalue(), '')
        self.assertEqual([call.args[0] for call in self.serial.call_args_list],
                         [guest.INSTALL_PREFIX, *([guest.TLS_PREFIX] * 3), *([guest.NATIVE_TLS_PREFIX] * 2),
                          guest.OBSERVATIONS_PREFIX, guest.PREFIX])
        self.assertIn('--setenv=NIX_SSL_CERT_FILE=' + guest.SYSTEM_CA_BUNDLE, argv)
        self.assertEqual(self.serial.call_args.args, (guest.PREFIX, result))

    def test_seed_metadata_cross_mode_rejected_before_installer_or_export_io(self):
        for config, meta in ((network_config(), metadata()), (CONFIG, self.meta)):
            with patch.object(guest, 'guard'), patch.object(guest, 'system_ca_metadata'), \
                 patch.object(guest.os.path, 'lexists', return_value=False), \
                 patch.object(Path, 'mkdir'), patch.object(guest, 'checked', return_value=b''), \
                 patch.object(guest, 'payload_fd', return_value=10) as payload, patch.object(guest.os, 'close'), \
                 patch.object(guest.os, 'read', return_value=json.dumps(meta).encode()), \
                 patch.object(guest, 'install_official') as install, self.assertRaisesRegex(guest.GuestFailure, 'metadata-transport'):
                guest.root_stages(config)
            self.assertEqual(payload.call_count, 1)
            install.assert_not_called()

    def test_user_network_context_reentry_and_receipt_preserve_real_lifecycle(self):
        meta = self.meta
        context = json.dumps({'metadata': meta, 'tools': self.tools}).encode()
        fixture = receipt_fixtures.ReceiptTests()
        fixture.setUp()
        for entry in ('user', 'lifecycle'):
            captured = []
            def nix(args, env, **kwargs):
                self.assertEqual(env['NIX_CONFIG'], guest.nix_config('guest-fetch'))
                self.assertIn('--no-update-lock-file', args)
                captured.append(args)
                return json.dumps([{'outputs': {'out': store('setup')}}]).encode() if args[0] == 'build' else b'{}'
            python = store('python3-env') + '/bin/python3'
            reads = [context] + [(store('python3-env') + '/bin').encode()] if entry == 'user' else [context]
            with patch.object(guest, 'guard'), patch.object(guest, 'private_read', side_effect=reads), \
                 patch.object(guest, 'verify_tools') as verify, patch.dict(guest.os.environ, {}, clear=True), \
                 patch.object(guest, 'checked', return_value=b'active'), patch.object(guest, 'prepare_checkout') as prepare, \
                 patch.object(guest, 'normalize_seed_identity') as normalize, patch.object(guest, 'nix', side_effect=nix), \
                 patch.object(Path, 'is_file', return_value=True), patch.object(guest.sys, 'argv', [guest.SCRIPT, entry]), \
                 patch.object(guest.os, 'execve', side_effect=KeyboardInterrupt) as execute, \
                 patch.object(guest, 'git', return_value=b'revision'), \
                 patch.object(guest, 'lifecycle', return_value=(fixture.row['stages'], None)) as lifecycle, \
                 patch.object(Path, 'mkdir'), patch.object(guest, 'write_new') as write:
                if entry == 'user':
                    with self.assertRaises(KeyboardInterrupt):
                        guest.user_stages(network_config())
                    self.assertEqual(execute.call_args.args[:2], (python, [python, guest.SCRIPT, 'lifecycle']))
                    self.assertEqual(execute.call_args.args[2]['NIX_CONFIG'], guest.nix_config('guest-fetch'))
                    self.assertEqual(execute.call_args.args[2]['ENVY_RUNTIME_LOCK_SHA256'], meta['source']['lockSha256'])
                    prepare.assert_called_once()
                    normalize.assert_not_called()
                    lifecycle.assert_not_called()
                else:
                    row = guest.user_stages(network_config())
                    self.assertEqual(row['stages'], fixture.row['stages'])
                    self.assertEqual(row['transport'], 'guest-fetch')
                    self.assertEqual(row['source'], meta['source'])
                    self.assertEqual(set(row['source']), {'path', 'narHash', 'lockSha256', 'manifestSha256'})
                    self.assertEqual(row['evidence'], 'linux-vm-envy-lifecycle-networked')
                    lifecycle.assert_called_once()
                    self.assertEqual(lifecycle.call_args.args[0]['NIX_CONFIG'], guest.nix_config('guest-fetch'))
                    self.assertEqual(json.loads(write.call_args.args[1]), row)
                    prepare.assert_not_called()
                    normalize.assert_called_once()
                verify.assert_called_once_with(self.tools)
        bad_config = {**network_config(), 'transport': 'nix-export'}
        with patch.object(guest, 'guard'), patch.object(guest, 'private_read', return_value=context), \
             patch.object(guest, 'verify_tools') as verify, self.assertRaisesRegex(guest.GuestFailure, 'metadata-transport'):
            guest.user_stages(bad_config)
        verify.assert_not_called()

    def test_archive_or_root_setup_lock_drift_stops_before_next_repository_phase(self):
        actual = {'path': store('source'), 'narHash': self.meta['source']['narHash'], 'narSize': 100, 'references': []}
        env = {**self.env, 'ENVY_RUNTIME_LOCK_SHA256': guest.sha(b'fixed')}
        for phase in ('archive', 'setup'):
            events = []
            changed = [False]
            def checked(argv, **kwargs):
                if 'add-path' in argv:
                    return (store('source') + '\n').encode()
                if 'path-info' in argv:
                    return json.dumps([actual]).encode()
                if 'archive' in argv:
                    events.append('archive')
                    changed[0] = phase == 'archive'
                    return json.dumps({'path': store('source')}).encode()
                if 'build' in argv:
                    events.append('setup')
                    changed[0] = True
                    return b''
                raise AssertionError('later repository phase after lock drift')
            with self.subTest(phase=phase), patch.object(guest, 'checked', side_effect=checked), \
                 patch.object(guest, 'private_read', side_effect=lambda *a, **kw: b'changed' if changed[0] else b'fixed'), \
                 self.assertRaisesRegex(guest.GuestFailure, 'lock-drift'):
                guest.fetch_payload(self.meta, env, Path('/guest/new-source'), network_config(self.meta))
            self.assertEqual(events, ['archive'] if phase == 'archive' else ['archive', 'setup'])

    def test_actual_setup_lock_drift_is_partial_and_stops_lifecycle(self):
        with patch.object(guest, 'command', return_value=(0, b'')), \
             patch.object(guest, 'private_read', return_value=b'changed'), patch.object(guest, 'apply_stage') as apply:
            stages, error = guest.lifecycle(self.env, b'rev')
        self.assertEqual(error, 'failed-or-unknown:setup')
        self.assertEqual(set(stages), {'setup'})
        self.assertEqual(stages['setup'], guest.SetupDialogue().failure_observation(guest.GuestFailure('lock-drift')))
        apply.assert_not_called()

    def test_cross_mode_payload_helpers_fail_before_nix_or_signature_override(self):
        for function, args in ((guest.import_payload, (77, self.meta, self.env)),
                               (guest.import_payload, (77, metadata(), self.env)),
                               (guest.fetch_payload, (self.meta, guest.environment(), Path('/guest/source'), network_config(self.meta))),
                               (guest.bootstrap_tools, (self.meta, guest.environment()))):
            with self.subTest(function=function.__name__), patch.object(guest, 'nix') as nix, \
                 patch.object(guest, 'checked') as checked, self.assertRaises(guest.GuestFailure):
                function(*args)
            nix.assert_not_called()
            checked.assert_not_called()

    def test_import_and_rendered_compile_have_zero_host_io(self):
        source = Path(guest.__file__).read_text()
        program = guest.guest_program(TOKEN, self.meta)
        with patch('builtins.open', side_effect=AssertionError('host open')), \
             patch.object(guest.os, 'open', side_effect=AssertionError('host os.open')), \
             patch.object(Path, 'read_text', side_effect=AssertionError('host read')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('host read')), \
             patch.object(Path, 'stat', side_effect=AssertionError('host stat')), \
             patch.object(Path, 'iterdir', side_effect=AssertionError('host directory')):
            for raw in (source, program):
                exec(compile(raw, '<inert guest>', 'exec'), {'__name__': 'zero_io_guest'})


class GithubPrefetchTests(Isolated):
    REAL_PREFETCH = True

    def setUp(self):
        super().setUp()
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))
        self.fixture = Path('/synthetic/prefetch-fixture')
        self.lock = {'version': 7, 'root': 'root', 'nodes': {
            'root': {'inputs': {'dep': 'dep'}},
            'dep': {'locked': {'type': 'github', 'owner': 'NixOS', 'repo': 'nixpkgs',
                               'rev': 'a' * 40, 'narHash': sri(b'locked input'), 'lastModified': 1},
                    'original': {'type': 'github', 'owner': 'NixOS', 'repo': 'nixpkgs'}}}}
        self.bind(self.lock)
        self.read = self.enterContext(patch.object(guest, 'private_read', side_effect=self.read_lock))
        self.tools = {name: store(name) for name in ('gitMinimal', 'sops', 'age', 'ssh-to-age')}
        self.calls = []
        self.now = 1
        self.canonical = '/nix/store/' + 'c' * 32 + '-source'
        self.store_checked = self.enterContext(patch.object(guest, 'checked', side_effect=self.fixed_native))
        self.enterContext(patch.object(guest.time, 'monotonic', side_effect=lambda: self.now))

    def bind(self, lock=None, *, raw=None):
        self.raw = json.dumps(lock).encode() if raw is None else raw
        self.meta = network_metadata()
        self.meta['source']['lockSha256'] = guest.sha(self.raw)
        self.env = guest.environment(transport='guest-fetch', lock_sha256=guest.sha(self.raw))

    def read_lock(self, path, cap, **kwargs):
        self.assertIn(path, (self.fixture / 'flake.lock', Path(store('source')) / 'flake.lock'))
        self.assertIn(cap, (guest.MAX_METADATA, guest.MAX_SOURCE))
        return self.raw

    def fixed_native(self, argv, **kwargs):
        self.assertEqual(argv[:4], [guest.STORE, '--option', 'builders', ''])
        self.assertIs(kwargs.pop('merge'), False)
        return self.native(argv[4:], kwargs.pop('env'), **kwargs)

    def native(self, args, env, **kwargs):
        self.assertIs(env, self.env)
        self.calls.append((list(args), dict(kwargs), guest.DEADLINE))
        if args[0] == '--print-fixed-path':
            self.assertEqual(args, ['--print-fixed-path', '--recursive', 'sha256',
                                   self.lock['nodes']['dep']['locked']['narHash'], 'source'])
            return (self.canonical + '\n').encode()
        if args[0] == 'copy':
            self.assertEqual(args, ['copy', '--from', 'https://cache.nixos.org',
                                   '--option', 'require-sigs', 'true', '--option', 'http2', 'false',
                                   '--log-format', 'internal-json', self.canonical])
            self.assertEqual(kwargs.get('_cache_copy_path'), self.canonical)
            return b''
        if args[:2] == ['store', 'add-path']:
            return (store('source') + '\n').encode()
        if args[:2] == ['store', 'prefetch-file']:
            path = self.canonical if args[-1].startswith('https://codeload.github.com/NixOS/nixpkgs/') else store('prefetched')
            return json.dumps({'storePath': path, 'hash': args[-2],
                               'ignored': 'PRIVATE RESPONSE'}).encode()
        if args[0] == 'path-info':
            source = args[-1] == store('source')
            return json.dumps([{'path': args[-1], 'narHash': self.meta['source']['narHash'] if source
                               else self.lock['nodes']['dep']['locked']['narHash'],
                                'narSize': 100, 'references': []}]).encode()
        if args[:2] == ['flake', 'archive']:
            return json.dumps({'path': store('source')}).encode()
        raise AssertionError('unexpected native operation')

    def fetch(self):
        return guest.fetch_payload(self.meta, self.env, self.fixture, network_config(self.meta))

    def prefetch_only(self):
        guest.prefetch_github_inputs(self.meta, self.env, self.fixture, network_config(self.meta))

    def assert_rejected_before_prefetch(self, lock=None, *, raw=None):
        self.bind(lock, raw=raw)
        with patch.object(guest, 'nix') as native, self.assertRaises((guest.GuestFailure, ValueError, TypeError)):
            self.prefetch_only()
        native.assert_not_called()
        self.assertEqual(guest.DEADLINE, 1000)

    def test_exact_official_url_native_flags_and_both_hashes_precede_original_archive(self):
        base = [guest.NIX, '--extra-experimental-features', 'nix-command flakes', '--option', 'builders', '']
        def checked(argv, **kwargs):
            if argv[0] == guest.STORE:
                return self.fixed_native(argv, **kwargs)
            self.assertEqual(argv[:len(base)], base)
            self.assertIs(kwargs.pop('env'), self.env)
            self.assertIs(kwargs.pop('merge'), False)
            return self.native(argv[len(base):], self.env, **kwargs)
        with patch.object(guest, 'checked', side_effect=checked), \
             patch.object(guest, 'bootstrap_tools', return_value=self.tools) as tools:
            self.assertEqual(self.fetch(), self.tools)
        ops = [call[0] for call in self.calls]
        locked = self.lock['nodes']['dep']['locked']
        self.assertEqual(ops, [
            ['store', 'add-path', '--name', 'source', str(self.fixture)],
            ['path-info', '--json', store('source')],
            ['--print-fixed-path', '--recursive', 'sha256', locked['narHash'], 'source'],
            ['copy', '--from', 'https://cache.nixos.org', '--option', 'require-sigs', 'true',
             '--option', 'http2', 'false', '--log-format', 'internal-json', self.canonical],
            ['path-info', '--json', self.canonical],
            ['store', 'prefetch-file', '--unpack', '--name', 'source', '--json', '--log-format', 'internal-json', '--expected-hash',
             locked['narHash'], 'https://codeload.github.com/NixOS/nixpkgs/tar.gz/' + 'a' * 40],
            ['path-info', '--json', self.canonical],
            ['flake', 'archive', '--json', '--no-update-lock-file', 'path:' + store('source')]])
        self.assertEqual([c[1].get('timeout') for c in self.calls], [None, None, 600, 600, 600, 600, 600, 1800])
        self.assertEqual([c[2] for c in self.calls], [1000, 1000, 601, 601, 601, 601, 601, 1000])
        self.assertEqual(guest.DEADLINE, 1000)
        tools.assert_called_once_with(self.meta, self.env)
        self.assertEqual(self.env['NIX_SSL_CERT_FILE'], guest.SYSTEM_CA_BUNDLE)
        self.assertIn('ssl-verify = true', self.env['NIX_CONFIG'])
        self.assertIn('require-sigs = true', self.env['NIX_CONFIG'])
        self.assertEqual([c.args[1]['step'] for c in self.serial.call_args_list], ['start', 'verified'])
        self.assertFalse(any('override' in arg or 'update-lock' == arg for op in ops for arg in op))

    def test_deduplication_order_and_other_types_are_left_to_native_archive(self):
        duplicate = copy.deepcopy(self.lock['nodes']['dep'])
        self.lock['nodes']['duplicate'] = duplicate
        self.lock['nodes']['earlier'] = copy.deepcopy(duplicate)
        self.lock['nodes']['earlier']['locked']['owner'] = 'AAA'
        self.lock['nodes']['earlier']['original']['owner'] = 'AAA'
        self.lock['nodes']['tar'] = {'locked': {'type': 'tarball', 'url': 'PRIVATE untouched URL'}}
        self.bind(self.lock)
        with patch.object(guest, 'nix', side_effect=self.native):
            self.prefetch_only()
        urls = [c[0][-1] for c in self.calls if c[0][0] == 'store']
        self.assertEqual(urls, ['https://codeload.github.com/AAA/nixpkgs/tar.gz/' + 'a' * 40,
                                'https://codeload.github.com/NixOS/nixpkgs/tar.gz/' + 'a' * 40])
        self.assertNotIn('PRIVATE', json.dumps(self.calls))

    def test_schema_root_nodes_bool_and_duplicate_json_are_rejected(self):
        bad_locks = [None, [], True, {}, {**self.lock, 'version': True}, {**self.lock, 'version': 8},
                     {**self.lock, 'extra': 1}, {**self.lock, 'root': True}, {**self.lock, 'root': 'absent'},
                     {**self.lock, 'nodes': []}, {**self.lock, 'nodes': {}},
                     {**self.lock, 'nodes': {'root': False}},
                     {**self.lock, 'nodes': {'root': {'inputs': False}}},
                     {**self.lock, 'nodes': {'root': {'inputs': {'x': False}}}},
                     {**self.lock, 'nodes': {'root': {'inputs': {'x': [True]}}}},
                     {**self.lock, 'nodes': {'root': {'flake': 1}}},
                     {**self.lock, 'nodes': {'root': {'locked': {}}}},
                     {**self.lock, 'nodes': {**self.lock['nodes'], 'z': {'locked': {'type': False}}}}]
        for lock in bad_locks:
            with self.subTest(lock=lock):
                self.assert_rejected_before_prefetch(lock)
        for raw in (b'{"version":7,"version":7,"root":"root","nodes":{"root":{}}}',
                    b'{"version":7,"root":"root","nodes":{"root":{},"root":{}}}',
                    b'{"version":7,"root":"root","nodes":{"root":{"inputs":{"a":"root","a":"root"}}}}',
                    b'{"version":NaN}', b'false', b'{'):
            with self.subTest(raw=raw):
                self.assert_rejected_before_prefetch(raw=raw)

    def test_missing_fields_unsafe_owner_repo_revision_and_hash_never_fetch(self):
        changes = {'host': ['evil.example', 'github.com/x', 'api.github.com', False, None],
                   'owner': ['', False, '../x', 'a/b', 'a?x', 'a#x', 'a%2fx', 'a\\b', '中', '-a'],
                   'repo': ['', True, '..', 'a..b', 'a/b', 'a?x', 'a#x', 'a%2fb', 'a\\b', '中'],
                   'rev': [False, '', 'a' * 39, 'A' * 40, 'g' * 40, 'a' * 40 + '?x'],
                   'narHash': [False, '', 'sha512-' + 'A' * 43 + '=',
                               'sha256-' + 'A' * 42 + '=', 'sha256-' + 'A' * 44,
                               'sha256-' + 'A' * 42 + 'B=', sri(b'ok') + '?x'],
                   'lastModified': [True, -1, '1', None]}
        for field, values in changes.items():
            for value in values:
                lock = copy.deepcopy(self.lock)
                lock['nodes']['dep']['locked'][field] = value
                with self.subTest(field=field, value=value):
                    self.assert_rejected_before_prefetch(lock)
        for field in ('type', 'owner', 'repo', 'rev', 'narHash'):
            lock = copy.deepcopy(self.lock)
            del lock['nodes']['dep']['locked'][field]
            with self.subTest(missing=field):
                self.assert_rejected_before_prefetch(lock)

    def test_unsupported_content_attributes_in_locked_or_original_do_not_downgrade(self):
        for where in ('locked', 'original'):
            for attr in ('dir', 'submodules', 'allRefs', 'shallow', 'url', 'lfs'):
                lock = copy.deepcopy(self.lock)
                lock['nodes']['dep'][where][attr] = False
                with self.subTest(where=where, attr=attr):
                    self.assert_rejected_before_prefetch(lock)
        for change in ({'host': 'evil.example'}, {'owner': 'other'}, {'type': 'git'}, {'ref': True}):
            lock = copy.deepcopy(self.lock)
            lock['nodes']['dep']['original'].update(change)
            with self.subTest(original=change):
                self.assert_rejected_before_prefetch(lock)

    def test_all_nodes_validate_before_first_fetch_including_late_invalid_node(self):
        lock = copy.deepcopy(self.lock)
        lock['nodes']['z'] = copy.deepcopy(lock['nodes']['dep'])
        lock['nodes']['z']['locked']['dir'] = 'subdir'
        self.assert_rejected_before_prefetch(lock)

    def test_node_and_github_count_caps_apply_before_prefetch_and_duplicates_count_once(self):
        lock = copy.deepcopy(self.lock)
        for i in range(63):
            lock['nodes']['dup' + str(i)] = copy.deepcopy(lock['nodes']['dep'])
        self.bind(lock)
        with patch.object(guest, 'nix', side_effect=self.native) as native:
            self.prefetch_only()
        self.assertEqual(native.call_count, 4)
        lock['nodes']['over'] = copy.deepcopy(lock['nodes']['dep'])
        self.assert_rejected_before_prefetch(lock)
        lock = {'version': 7, 'root': 'root', 'nodes': {'root': {}}}
        lock['nodes'].update({str(i): {} for i in range(128)})
        self.assert_rejected_before_prefetch(lock)

    def test_empty_github_set_performs_no_prefetch(self):
        self.bind({'version': 7, 'root': 'root', 'nodes': {'root': {},
                   'tar': {'locked': {'type': 'tarball', 'url': 'https://api.github.com/fixed'}}}})
        with patch.object(guest, 'nix') as native:
            self.prefetch_only()
        native.assert_not_called()

    def test_unknown_or_mismatched_native_result_never_enters_archive(self):
        for result in (None, [], False, {}, {'storePath': False, 'hash': sri(b'locked input')},
                       {'storePath': '/nix/store/../private', 'hash': sri(b'locked input')},
                       {'storePath': self.canonical, 'hash': False},
                       {'storePath': self.canonical, 'hash': sri(b'wrong')},
                       {'storePath': self.canonical}):
            self.calls = []
            def native(args, env, **kwargs):
                if args[:2] == ['store', 'prefetch-file']:
                    self.calls.append((args, kwargs, guest.DEADLINE))
                    return json.dumps(result).encode()
                return self.native(args, env, **kwargs)
            with self.subTest(result=result), patch.object(guest, 'nix', side_effect=native), \
                 patch.object(guest, 'bootstrap_tools') as tools, self.assertRaises(guest.GuestFailure):
                self.fetch()
            self.assertFalse(any(c[0][0] == 'flake' for c in self.calls))
            tools.assert_not_called()
            self.assertEqual(guest.DEADLINE, 1000)

    def test_native_path_info_hash_mismatch_or_unknown_stops_original_archive(self):
        for output in ([], {}, [{'path': self.canonical, 'narHash': sri(b'wrong'),
                                 'narSize': 1, 'references': []}]):
            self.calls = []
            def native(args, env, **kwargs):
                if args == ['path-info', '--json', self.canonical]:
                    return json.dumps(output).encode()
                return self.native(args, env, **kwargs)
            with patch.object(guest, 'nix', side_effect=native), \
                 self.assertRaises((guest.GuestFailure, KeyError, TypeError)):
                self.fetch()
            self.assertFalse(any(c[0][0] == 'flake' for c in self.calls))
            self.assertEqual(guest.DEADLINE, 1000)

    def test_initial_fixture_source_and_environment_fingerprints_must_match(self):
        for where in ('fixture', 'source', 'env'):
            def read(path, *a, **kw):
                self.read_lock(path, *a, **kw)
                return b'changed' if path == (self.fixture if where == 'fixture' else Path(store('source'))) / 'flake.lock' else self.raw
            env = {**self.env, 'ENVY_RUNTIME_LOCK_SHA256': 'f' * 64} if where == 'env' else self.env
            with self.subTest(where=where), patch.object(guest, 'private_read', side_effect=read), \
                 patch.object(guest, 'nix') as native, self.assertRaisesRegex(guest.GuestFailure, 'lock-drift'):
                guest.prefetch_github_inputs(self.meta, env, self.fixture, network_config(self.meta))
            native.assert_not_called()
            self.assertEqual(guest.DEADLINE, 1000)

    def test_lock_drift_during_either_native_operation_stops_archive(self):
        for operation in ('prefetch-file', 'path-info'):
            for changed_path in (self.fixture, Path(store('source'))):
                drift = [False]
                self.calls = []
                def native(args, env, **kwargs):
                    output = self.native(args, env, **kwargs)
                    if operation in args and (operation == 'prefetch-file' or args[-1] == self.canonical):
                        drift[0] = True
                    return output
                def read(path, *a, **kw):
                    self.read_lock(path, *a, **kw)
                    return b'changed PRIVATE' if drift[0] and path == changed_path / 'flake.lock' else self.raw
                with self.subTest(operation=operation, changed_path=changed_path), \
                     patch.object(guest, 'private_read', side_effect=read), patch.object(guest, 'nix', side_effect=native), \
                     self.assertRaisesRegex(guest.GuestFailure, 'lock-drift'):
                    self.fetch()
                self.assertFalse(any(c[0][0] == 'flake' for c in self.calls))
                self.assertEqual(guest.DEADLINE, 1000)

    def test_single_input_budget_includes_native_hash_recheck_and_restores_full_deadline(self):
        def native(args, env, **kwargs):
            output = self.native(args, env, **kwargs)
            self.now += {'store': 350, 'path-info': 250, 'copy': 250}.get(args[0], 0)
            return output
        with patch.object(guest, 'nix', side_effect=native), self.assertRaisesRegex(guest.GuestFailure, '^timeout$'):
            self.prefetch_only()
        self.assertEqual([c[1]['timeout'] for c in self.calls], [600, 600, 350, 100])
        self.assertEqual(guest.DEADLINE, 1000)

    def test_input_between_120_and_600_seconds_completes_with_shared_budget(self):
        def native(args, env, **kwargs):
            output = self.native(args, env, **kwargs)
            self.now += {'store': 200, 'path-info': 99, 'copy': 1}.get(args[0], 0)
            return output
        with patch.object(guest, 'nix', side_effect=native):
            self.prefetch_only()
        self.assertEqual([c[1]['timeout'] for c in self.calls], [600, 600, 599, 500, 300])
        self.assertEqual([c.args[1]['step'] for c in self.serial.call_args_list], ['start', 'verified'])
        self.assertEqual(guest.DEADLINE, 1000)

    def test_exact_and_exceeded_input_deadline_never_verify(self):
        for elapsed in (600, 601):
            self.now, self.calls = 1, []
            self.serial.reset_mock()
            def native(args, env, **kwargs):
                result = self.native(args, env, **kwargs)
                self.now += elapsed
                return result
            with self.subTest(elapsed=elapsed), patch.object(guest, 'nix', side_effect=native), \
                 self.assertRaisesRegex(guest.GuestFailure, '^timeout$'):
                self.prefetch_only()
            self.assertEqual([c.args[1]['step'] for c in self.serial.call_args_list], ['start'])
            self.assertEqual(guest.DEADLINE, 1000)

    def test_two_inputs_emit_only_closed_ordered_nonce_progress(self):
        self.lock['nodes']['other'] = copy.deepcopy(self.lock['nodes']['dep'])
        self.lock['nodes']['other']['locked']['repo'] = 'other'
        self.lock['nodes']['other']['original']['repo'] = 'other'
        self.bind(self.lock)
        with patch.object(guest, 'nix', side_effect=self.native):
            self.prefetch_only()
        rows = [c.args[1] for c in self.serial.call_args_list]
        self.assertEqual(rows, [
            {'schemaVersion': 1, 'token': TOKEN, 'currentIndex': i, 'totalInputs': 2,
             'completedInputs': i - (step == 'start'), 'step': step}
            for i in (1, 2) for step in ('start', 'verified')])
        self.assertTrue(all(c.args[0] == guest.PREFETCH_PREFIX for c in self.serial.call_args_list))
        self.assertNotIn('PRIVATE', json.dumps(rows))
        self.assertNotIn('nixpkgs', json.dumps(rows))
        self.assertNotIn('/nix/store', json.dumps(rows))
        self.assertNotIn('sha256-', json.dumps(rows))

    def test_progress_rejects_invalid_nonce_config_bool_counts_and_limits(self):
        for change in ({'token': True}, {'token': 'bad'}, {'transport': 'unknown'},
                       {'transport': 'nix-export'}, {'extra': 1}, {'fullTimeout': True}):
            with self.subTest(change=change), self.assertRaises(guest.GuestFailure):
                guest.emit_prefetch_progress({**network_config(), **change}, 1, 1, 0, 'start')
        for values in ((True, 1, 0, 'start'), (1, True, 0, 'start'), (1, 1, False, 'start'),
                       (0, 1, 0, 'start'), (1, 65, 0, 'start'), (2, 1, 1, 'start'),
                       (1, 1, 1, 'start'), (1, 1, 0, 'verified'), (1, 1, 0, 'done')):
            with self.subTest(values=values), self.assertRaises(guest.GuestFailure):
                guest.emit_prefetch_progress(network_config(), *values)
        for step in ('start', 'verified'):
            guest.emit_prefetch_progress(network_config(), 64, 64, 63 if step == 'start' else 64, step)
        self.assertEqual(self.serial.call_count, 2)

    def test_invalid_config_and_source_nar_binding_fail_before_progress(self):
        for config in (None, {}, {**network_config(), 'token': False},
                       {**network_config(), 'transport': 'unknown'}):
            with self.subTest(config=config), patch.object(guest, 'nix') as native, \
                 self.assertRaises((guest.GuestFailure, TypeError)):
                guest.prefetch_github_inputs(self.meta, self.env, self.fixture, config)
            native.assert_not_called()
        for change in ({'narHash': sri(b'wrong')}, {'narSize': 101}):
            def native(args, env, **kwargs):
                result = self.native(args, env, **kwargs)
                if args == ['path-info', '--json', store('source')]:
                    result = json.dumps([{**json.loads(result)[0], **change}]).encode()
                return result
            with patch.object(guest, 'nix', side_effect=native), self.assertRaises(guest.GuestFailure):
                self.fetch()
        self.serial.assert_not_called()

    def test_hash_lock_and_command_errors_keep_last_start_without_archive(self):
        for failure in ('hash', 'lock', 'timeout', 'cancel'):
            self.now, self.calls = 1, []
            self.serial.reset_mock()
            drift = [False]
            def native(args, env, **kwargs):
                result = self.native(args, env, **kwargs)
                if args == ['path-info', '--json', self.canonical]:
                    if failure == 'hash':
                        return json.dumps([{**json.loads(result)[0], 'narHash': sri(b'wrong')}]).encode()
                    if failure == 'lock':
                        drift[0] = True
                    elif failure == 'timeout':
                        raise guest.GuestFailure('command-timeout')
                    else:
                        raise KeyboardInterrupt
                return result
            with patch.object(guest, 'nix', side_effect=native), \
                 patch.object(guest, 'private_read', side_effect=lambda *a, **k: b'PRIVATE' if drift[0] else self.raw), \
                 self.assertRaises((guest.GuestFailure, KeyboardInterrupt)):
                self.fetch()
            self.assertEqual([c.args[1]['step'] for c in self.serial.call_args_list], ['start'])
            self.assertEqual(self.serial.call_args.args[1]['completedInputs'], 0)
            self.assertFalse(any(c[0][0] == 'flake' for c in self.calls))
            self.assertEqual(guest.DEADLINE, 1000)

    def test_serial_failure_restores_deadline_and_stops_archive(self):
        for after in (0, 1):
            self.calls = []
            self.serial.reset_mock()
            self.serial.side_effect = [None] * after + [OSError('PRIVATE serial')]
            with patch.object(guest, 'nix', side_effect=self.native), self.assertRaises(OSError):
                self.fetch()
            self.assertFalse(any(c[0][0] == 'flake' for c in self.calls))
            self.assertEqual(guest.DEADLINE, 1000)
            self.assertEqual(self.serial.call_count, after + 1)

    def test_start_serial_elapsed_full_budget_stops_before_native_download(self):
        def serial(*args):
            self.now += 10
        with patch.object(guest, 'DEADLINE', 5), patch.object(guest, 'emit_serial', side_effect=serial), \
             patch.object(guest, 'nix') as native, self.assertRaisesRegex(guest.GuestFailure, '^timeout$'):
            self.prefetch_only()
        native.assert_not_called()
        self.assertEqual(guest.DEADLINE, 1000)

    def test_progress_user_offline_and_expired_deadline_have_no_serial(self):
        for uid, config, deadline in ((1000, network_config(), 1000), (0, CONFIG, 1000),
                                      (0, network_config(), 0)):
            with patch.object(guest.os, 'getuid', return_value=uid), patch.object(guest, 'DEADLINE', deadline), \
                 self.assertRaises(guest.GuestFailure):
                guest.emit_prefetch_progress(config, 1, 1, 0, 'start')
        self.serial.assert_not_called()

    def test_stage_budget_is_1800_and_remains_inside_full_deadline(self):
        lock = copy.deepcopy(self.lock)
        for i in range(16):
            node = copy.deepcopy(lock['nodes']['dep'])
            node['locked']['repo'] = node['original']['repo'] = 'repo' + str(i)
            lock['nodes']['n' + str(i)] = node
        self.bind(lock)
        def native(args, env, **kwargs):
            output = self.native(args, env, **kwargs)
            if args[0] == 'store':
                self.now += min(110, kwargs['timeout'])
            return output
        for full in (7201, 101):
            self.now, self.calls = 1, []
            with patch.object(guest, 'DEADLINE', full), patch.object(guest, 'nix', side_effect=native), \
                 self.assertRaisesRegex(guest.GuestFailure, '^timeout$'):
                self.prefetch_only()
                self.fail('budget did not stop prefetch')
            self.assertTrue(all(c[2] <= min(full, 1801) for c in self.calls))
            self.assertTrue(all(0 < c[1]['timeout'] <= 600 for c in self.calls))
        self.assertEqual(guest.DEADLINE, 1000)

    def test_fetch_failure_timeout_output_limit_cancel_restore_deadline_and_stop_archive(self):
        for operation in ('prefetch-file', 'path-info'):
            for error in (guest.GuestFailure('command-failed'), guest.GuestFailure('nix-hash-mismatch'),
                          guest.GuestFailure('command-timeout'), guest.GuestFailure('command-output-limit'), KeyboardInterrupt()):
                self.calls = []
                def native(args, env, **kwargs):
                    if operation in args and (operation == 'prefetch-file' or args[-1] == self.canonical):
                        raise error
                    return self.native(args, env, **kwargs)
                with self.subTest(operation=operation, error=type(error)), patch.object(guest, 'nix', side_effect=native), \
                     patch.object(guest, 'bootstrap_tools') as tools, self.assertRaises(type(error)):
                    self.fetch()
                tools.assert_not_called()
                self.assertFalse(any(c[0][0] == 'flake' for c in self.calls))
                self.assertEqual(guest.DEADLINE, 1000)
                self.assertEqual(guest.ROOT_PHASE, 'input-prefetch')

    def test_root_phase_closed_diagnostics_and_poweroff_survive_prefetch_failure(self):
        for error in (guest.GuestFailure('input-prefetch-hash'), KeyboardInterrupt()):
            def fail(config):
                guest.ROOT_PHASE = 'input-prefetch'
                raise error
            with patch.object(guest.sys, 'argv', [guest.SCRIPT]), patch.object(guest.signal, 'signal'), \
                 patch.object(guest, 'guard'), patch.object(guest, 'root_stages', side_effect=fail), \
                 patch.object(guest, 'checked') as checked:
                self.assertIsNone(guest.main(network_config(self.meta)))
            self.assertEqual(checked.call_args.args[0], ['/usr/bin/systemctl', 'poweroff', '--no-block'])
            row = self.serial.call_args.args[1]
            self.assertEqual(row['phase'], 'input-prefetch')
            self.assertEqual(row['error'], 'cancelled' if isinstance(error, KeyboardInterrupt) else error.args[0])
            self.assertEqual(set(row), {'schemaVersion', 'token', 'phase', 'error', 'lastCommands'})
            self.assertNotIn('PRIVATE', json.dumps(row))
        self.assertIn('input-prefetch', guest.NIX_FAILURE_PHASES)

    def test_user_offline_and_expired_deadline_have_zero_new_effects(self):
        for uid, env, deadline in ((1000, self.env, 1000), (0, guest.environment(), 1000), (0, self.env, 0)):
            self.read.reset_mock()
            with patch.object(guest.os, 'getuid', return_value=uid), patch.object(guest, 'DEADLINE', deadline), \
                 patch.object(guest, 'nix') as native, self.assertRaises(guest.GuestFailure):
                guest.prefetch_github_inputs(self.meta, env, self.fixture, network_config(self.meta))
            self.read.assert_not_called()
            native.assert_not_called()
        self.serial.assert_not_called()


    def test_cache_branch_rejects_malformed_or_foreign_canonical_before_copy(self):
        outputs = (b'', b'false', b'/nix/store/../source\n',
                   (self.canonical + '\n' + self.canonical + '\n').encode(),
                   (' ' + self.canonical + '\n').encode(),
                   (self.canonical + '/child\n').encode(),
                   (store('foreign') + '\n').encode(), b'\xff')
        for output in outputs:
            self.calls = []
            def fixed(argv, **kwargs):
                self.fixed_native(argv, **kwargs)
                return output
            with self.subTest(output=output), patch.object(guest, 'checked', side_effect=fixed), \
                 patch.object(guest, 'nix', side_effect=self.native), self.assertRaises((guest.GuestFailure, UnicodeError)):
                self.prefetch_only()
            self.assertEqual([c[0][0] for c in self.calls], ['--print-fixed-path'])
            self.assertEqual(guest.DEADLINE, 1000)

    def test_cache_branch_same_hash_different_complete_prefetch_path_is_rejected(self):
        for path in (store('foreign') + '-source', store('foreign')):
            self.calls = []
            def native(args, env, **kwargs):
                result = self.native(args, env, **kwargs)
                if args[:2] == ['store', 'prefetch-file']:
                    return json.dumps({**json.loads(result), 'storePath': path}).encode()
                return result
            with self.subTest(path=path), patch.object(guest, 'nix', side_effect=native), \
                 self.assertRaisesRegex(guest.GuestFailure, '^input-prefetch-result$'):
                self.prefetch_only()
            self.assertEqual([c[0][0] for c in self.calls],
                             ['--print-fixed-path', 'copy', 'path-info', 'store'])
            self.assertEqual(guest.DEADLINE, 1000)

    def test_cache_branch_rejects_native_canonical_hash_path_and_size_before_prefetch(self):
        for change in ({'narHash': sri(b'foreign')}, {'path': store('foreign') + '-source'},
                       {'narSize': True}, {'narSize': 0}, {'references': ['foreign']}):
            self.calls = []
            def native(args, env, **kwargs):
                result = self.native(args, env, **kwargs)
                if args[0] == 'path-info':
                    return json.dumps([{**json.loads(result)[0], **change}]).encode()
                return result
            with self.subTest(change=change), patch.object(guest, 'nix', side_effect=native), \
                 self.assertRaises(guest.GuestFailure):
                self.prefetch_only()
            self.assertEqual([c[0][0] for c in self.calls], ['--print-fixed-path', 'copy', 'path-info'])

    def test_cache_branch_path_info_requires_one_complete_path_in_both_native_formats(self):
        row = {'narHash': self.lock['nodes']['dep']['locked']['narHash'], 'narSize': 100, 'references': []}
        outputs = (None, [], {}, [row], [dict(row, path=self.canonical)] * 2,
                   {Path(self.canonical).name: row}, {'info': {Path(self.canonical).name: row}},
                   {self.canonical: dict(row, path=store('foreign'))},
                   {self.canonical: row, store('foreign'): row})
        for output in outputs:
            self.calls = []
            def native(args, env, **kwargs):
                result = self.native(args, env, **kwargs)
                return json.dumps(output).encode() if args[0] == 'path-info' else result
            with self.subTest(output=output), patch.object(guest, 'nix', side_effect=native), \
                 self.assertRaises(guest.GuestFailure):
                self.prefetch_only()
            self.assertEqual([c[0][0] for c in self.calls], ['--print-fixed-path', 'copy', 'path-info'])
        for output in ({self.canonical: row}, {'info': {self.canonical: row}},
                       [dict(row, path=self.canonical)]):
            self.calls = []
            def native(args, env, **kwargs):
                result = self.native(args, env, **kwargs)
                return json.dumps(output).encode() if args[0] == 'path-info' else result
            with self.subTest(valid=output), patch.object(guest, 'nix', side_effect=native):
                self.prefetch_only()
            self.assertEqual([c[0][0] for c in self.calls],
                             ['--print-fixed-path', 'copy', 'path-info', 'store', 'path-info'])

    def test_cache_branch_preserves_non_nixpkgs_original_sequence_and_path(self):
        for owner, repo in (('NixOS', 'other'), ('Other', 'nixpkgs'), ('nixos', 'nixpkgs')):
            self.calls = []
            self.serial.reset_mock()
            for where in ('locked', 'original'):
                self.lock['nodes']['dep'][where].update(owner=owner, repo=repo)
            self.bind(self.lock)
            with patch.object(guest, 'nix', side_effect=self.native):
                self.prefetch_only()
            self.assertEqual([c[0] for c in self.calls], [
                ['store', 'prefetch-file', '--unpack', '--name', 'source', '--json', '--log-format',
                 'internal-json', '--expected-hash', self.lock['nodes']['dep']['locked']['narHash'],
                 'https://codeload.github.com/' + owner + '/' + repo + '/tar.gz/' + 'a' * 40],
                ['path-info', '--json', store('prefetched')]])
            self.assertEqual([c[1]['timeout'] for c in self.calls], [600, 600])

    def test_cache_branch_all_five_commands_share_remaining_input_budget(self):
        original = self.native
        def native(args, env, **kwargs):
            result = original(args, env, **kwargs)
            self.now += 100
            return result
        with patch.object(self, 'native', side_effect=native), patch.object(guest, 'nix', side_effect=native):
            self.prefetch_only()
        self.assertEqual([c[1]['timeout'] for c in self.calls], [600, 500, 400, 300, 200])
        self.assertEqual([c[2] for c in self.calls], [601] * 5)
        self.assertEqual(guest.DEADLINE, 1000)

    def test_cache_branch_checks_lock_before_effect_and_after_every_native_step(self):
        for changed in (self.fixture, Path(store('source'))):
            for step in range(6):
                self.calls = []
                self.serial.reset_mock()
                drift = [False]
                original = self.native
                def native(args, env, **kwargs):
                    result = original(args, env, **kwargs)
                    if len(self.calls) == step:
                        drift[0] = True
                    return result
                def serial(*args):
                    if step == 0:
                        drift[0] = True
                def read(path, *args, **kwargs):
                    return b'drift' if drift[0] and path == changed / 'flake.lock' else self.raw
                with self.subTest(changed=changed, step=step), \
                     patch.object(self, 'native', side_effect=native), patch.object(guest, 'nix', side_effect=native), \
                     patch.object(guest, 'emit_serial', side_effect=serial), \
                     patch.object(guest, 'private_read', side_effect=read), \
                     self.assertRaisesRegex(guest.GuestFailure, '^lock-drift$'):
                    self.prefetch_only()
                self.assertEqual(len(self.calls), step)
                self.assertEqual(guest.DEADLINE, 1000)

    def test_cache_branch_native_copy_nonzero_and_signature_errors_stop_without_fallback(self):
        base = [guest.NIX, '--extra-experimental-features', 'nix-command flakes', '--option', 'builders', '']
        for code, failure in ((1, 'unknown'), (97, 'unknown'), (-15, 'unknown'), (1, 'nix-hash-mismatch')):
            self.calls = []
            def command(argv, **kwargs):
                # Native errors, including signature rejection, are propagated;
                # content addressed objects need not carry a signature in Nix.
                self.assertIs(kwargs.pop('env'), self.env)
                self.assertIs(kwargs.pop('merge'), False)
                if argv[0] == guest.STORE:
                    self.assertEqual(argv[:4], [guest.STORE, '--option', 'builders', ''])
                    args = argv[4:]
                else:
                    self.assertEqual(argv[:len(base)], base)
                    args = argv[len(base):]
                result = self.native(args, self.env, **kwargs)
                if args[0] == 'copy':
                    guest.LAST_NIX_FAILURE = failure
                    return code, b'signature rejected by native Nix'
                return 0, result
            # Use the real checked/nix wrappers, replacing only command IO.
            with self.subTest(code=code, failure=failure), \
                 patch.object(guest, 'checked', CHECKED), \
                 patch.object(guest, 'command', side_effect=command), \
                 self.assertRaisesRegex(guest.GuestFailure,
                                        '^' + ('command-failed' if failure == 'unknown' else failure) + '$'):
                self.prefetch_only()
            self.assertEqual([c[0][0] for c in self.calls], ['--print-fixed-path', 'copy'])
            self.assertEqual(guest.DEADLINE, 1000)

    def test_cache_branch_failure_cancel_and_finally_lock_drift_preserve_first_error(self):
        for step in range(1, 6):
            for error in (guest.GuestFailure('command-timeout'), guest.GuestFailure('command-output-limit'),
                          KeyboardInterrupt(), OSError('native failure')):
                self.calls = []
                drift = [False]
                original = self.native
                def native(args, env, **kwargs):
                    result = original(args, env, **kwargs)
                    if len(self.calls) == step:
                        drift[0] = True
                        raise error
                    return result
                with self.subTest(step=step, error=type(error)), \
                     patch.object(self, 'native', side_effect=native), patch.object(guest, 'nix', side_effect=native), \
                     patch.object(guest, 'private_read', side_effect=lambda *a, **k: b'drift' if drift[0] else self.raw), \
                     self.assertRaises(type(error)) as caught:
                    self.prefetch_only()
                self.assertIs(caught.exception, error)
                self.assertEqual(len(self.calls), step)
                self.assertEqual(guest.DEADLINE, 1000)


class PublicTLSTests(Isolated):
    def setUp(self):
        super().setUp()
        self.env = guest.environment(transport='guest-fetch')
        self.bundle = {'regularFile': True, 'rootOwned': True, 'notGroupOtherWritable': True}

    def test_fixed_bundle_metadata_is_nofollow_and_never_reads_bytes(self):
        with patch.object(guest.os, 'stat', return_value=SimpleNamespace(st_mode=stat.S_IFREG | 0o644, st_uid=0)) as metadata, \
             patch.object(guest.os, 'open', side_effect=AssertionError('no CA open')), \
             patch.object(Path, 'read_bytes', side_effect=AssertionError('no CA read')):
            self.assertEqual(guest.system_ca_metadata(), self.bundle)
        metadata.assert_called_once_with('/etc/ssl/certs/ca-certificates.crt', follow_symlinks=False)

    def test_bad_owner_type_writable_and_links_fail_before_install_or_commands(self):
        cases = [(stat.S_IFREG | 0o644, 1000, 'file-owner'),
                 (stat.S_IFLNK | 0o777, 0, 'file-type-or-size'),
                 (stat.S_IFDIR | 0o755, 0, 'file-type-or-size'),
                 (stat.S_IFIFO | 0o644, 0, 'file-type-or-size'),
                 (stat.S_IFREG | 0o664, 0, 'file-mode'),
                 (stat.S_IFREG | 0o646, 0, 'file-mode')]
        for mode, uid, code in cases:
            with self.subTest(mode=mode, uid=uid), patch.object(guest, 'guard'), \
                 patch.object(guest.os, 'stat', return_value=SimpleNamespace(st_mode=mode, st_uid=uid)), \
                 patch.object(guest, 'command') as command, patch.object(guest, 'install_official') as install, \
                 patch.object(guest.os, 'open', side_effect=AssertionError('no CA open')), \
                 self.assertRaisesRegex(guest.GuestFailure, '^' + code + '$'):
                guest.root_stages(network_config())
            command.assert_not_called()
            install.assert_not_called()
            self.serial.assert_not_called()

    def test_guard_rejection_precedes_metadata_in_both_modes(self):
        for config in (CONFIG, network_config()):
            with patch.object(guest, 'guard', side_effect=guest.GuestFailure('guest-nonce')), \
                 patch.object(guest.os, 'stat', side_effect=AssertionError('metadata before guard')), \
                 self.assertRaisesRegex(guest.GuestFailure, 'guest-nonce'):
                guest.root_stages(config)

    def test_metadata_oserror_is_not_serialized(self):
        with patch.object(guest, 'guard'), patch.object(guest.os, 'stat', side_effect=OSError('SYNTHETIC-PRIVATE')), \
             patch.object(guest, 'install_official') as install, self.assertRaises(OSError) as caught:
            guest.root_stages(network_config())
        guest.emit_diagnostic(network_config(), caught.exception)
        self.assertEqual(self.serial.call_args.args[1]['error'], 'unknown')
        self.assertNotIn('SYNTHETIC', json.dumps(self.serial.call_args.args))
        install.assert_not_called()

    def test_three_fixed_head_commands_have_exact_budgets_and_closed_serial(self):
        expected = [('nix-cache', 'https://cache.nixos.org/nix-cache-info'),
                    ('github-api', 'https://api.github.com/'),
                    ('github-codeload', 'https://codeload.github.com/')]
        self.assertEqual(list(guest.PUBLIC_TLS_ENDPOINTS), expected)
        with patch.dict(guest.os.environ, {'https_proxy': 'PRIVATE', 'CURL_CA_BUNDLE': '/private/ca',
                                          'NIX_SSL_CERT_FILE': '/private/ca', 'HOME': '/private'}, clear=True), \
             patch.object(guest, 'command', side_effect=[(0, b'0 200\n'), (0, b'0 400\n'), (0, b'0 404\n')]) as command:
            guest.public_tls_probe(network_config(), self.env, self.bundle)
        self.assertEqual(command.call_count, 3)
        for i, (call, (label, url)) in enumerate(zip(command.call_args_list, expected)):
            self.assertEqual(call.args[0], ['/usr/bin/curl', '-q', '--head', '--output', '/dev/null',
                '--write-out', '%{ssl_verify_result} %{http_code}\n', '--silent', '--show-error',
                '--max-time', '12', '--proto', '=https', '--cacert', guest.SYSTEM_CA_BUNDLE, url])
            self.assertEqual(call.kwargs, {'env': {'LC_ALL': 'C.UTF-8'}, 'timeout': 12, 'merge': False})
            prefix, row = self.serial.call_args_list[i].args
            self.assertEqual(prefix, guest.TLS_PREFIX)
            self.assertEqual(row, {'schemaVersion': 1, 'token': TOKEN, 'bundle': self.bundle,
                'endpoint': label, 'exitCode': 0, 'sslVerifyResult': 0, 'httpStatus': (200, 400, 404)[i]})
            self.assertNotIn(url, json.dumps(row))

    def test_curl_tls_failures_keep_nonzero_exit_even_with_zero_verify_result(self):
        with patch.object(guest, 'command', side_effect=[(60, b'20 000\n'), (77, b'0 000\n'), (1, b'PRIVATE')]):
            guest.public_tls_probe(network_config(), self.env, self.bundle)
        rows = [call.args[1] for call in self.serial.call_args_list]
        self.assertEqual([(r['exitCode'], r['sslVerifyResult'], r['httpStatus']) for r in rows],
                         [(60, 20, None), (77, 0, None), (1, None, None)])
        self.assertNotIn('PRIVATE', json.dumps(rows))

    def test_metric_parser_refuses_bool_unknown_partial_and_response_text(self):
        for output in (True, False, None, 0, b'', b'false 200\n', b'0 true\n', b'0 200',
                       b'0 200\nPRIVATE', b'HTTP/2 200\n', b'-1 200\n', b'0 200\n0 200\n'):
            with self.subTest(output=output):
                self.assertEqual(guest.public_tls_values(output), (None, None))
        for raw, expected in ((b'0 404\n', (0, 404)), (b'0 400\n', (0, 400)),
                              (b'20 000\n', (20, None)), (b'0 999\n', (0, None)),
                              (b'9999999999 200\n', (None, 200))):
            self.assertEqual(guest.public_tls_values(raw), expected)

    def test_environment_bundle_binding_rejects_missing_changed_or_duplicate_setting(self):
        self.assertEqual(self.env['NIX_SSL_CERT_FILE'], guest.SYSTEM_CA_BUNDLE)
        self.assertEqual(self.env['NIX_CONFIG'].count('ssl-cert-file = '), 1)
        self.assertNotIn('NIX_SSL_CERT_FILE', guest.environment())
        for value in (None, '/private/ca', False):
            bad = dict(self.env)
            if value is None:
                del bad['NIX_SSL_CERT_FILE']
            else:
                bad['NIX_SSL_CERT_FILE'] = value
            with self.assertRaisesRegex(guest.GuestFailure, 'environment-mode-mismatch'):
                guest.env_transport(bad)
        for suffix in ('ssl-verify = false\n', 'require-sigs = false\n',
                       'ssl-cert-file = /private/ca\n', 'builders = ssh://private\n'):
            with self.assertRaisesRegex(guest.GuestFailure, 'environment-mode-mismatch'):
                guest.env_transport({**self.env, 'NIX_CONFIG': self.env['NIX_CONFIG'] + suffix})

    def test_host_ca_proxy_and_credentials_do_not_enter_user_or_root_environment(self):
        inherited = {'NIX_SSL_CERT_FILE': '/private/ca', 'SSL_CERT_FILE': '/private/ca',
                     'CURL_CA_BUNDLE': '/private/ca', 'https_proxy': 'PRIVATE', 'GH_TOKEN': 'PRIVATE'}
        with patch.dict(guest.os.environ, inherited, clear=True):
            for user in (False, True):
                env = guest.environment(transport='guest-fetch', user=user)
                self.assertEqual(env['NIX_SSL_CERT_FILE'], guest.SYSTEM_CA_BUNDLE)
                self.assertFalse((set(inherited) - {'NIX_SSL_CERT_FILE'}) & set(env))
                self.assertNotIn('PRIVATE', json.dumps(env))

    def test_cross_mode_bad_bundle_and_false_bool_exit_stop_before_serial(self):
        for config, env, bundle in ((CONFIG, guest.environment(), self.bundle),
                                   (network_config(), self.env, {**self.bundle, 'path': '/private'}),
                                   (network_config(), self.env, {**self.bundle, 'rootOwned': 1})):
            with patch.object(guest, 'command') as command, self.assertRaises(guest.GuestFailure):
                guest.public_tls_probe(config, env, bundle)
            command.assert_not_called()
        with patch.object(guest, 'command', return_value=(False, b'0 200\n')), self.assertRaises(guest.GuestFailure):
            guest.public_tls_probe(network_config(), self.env, self.bundle)
        self.serial.assert_not_called()

    def test_probe_timeout_cancel_and_serial_failure_stop_without_retry(self):
        for error in (guest.GuestFailure('command-timeout'), KeyboardInterrupt()):
            with patch.object(guest, 'command', side_effect=error) as command, \
                 self.assertRaises(type(error)):
                guest.public_tls_probe(network_config(), self.env, self.bundle)
            self.assertEqual(command.call_count, 1)
        self.serial.assert_not_called()
        self.serial.side_effect = guest.GuestFailure('serial-timeout')
        with patch.object(guest, 'command', return_value=(0, b'0 200\n')) as command, \
             self.assertRaisesRegex(guest.GuestFailure, 'serial-timeout'):
            guest.public_tls_probe(network_config(), self.env, self.bundle)
        self.assertEqual(command.call_count, 1)

    def test_actual_command_drains_stderr_without_logging_and_owns_group(self):
        process = Mock(pid=424242)
        process.stdout.fileno.return_value = 99
        process.stderr.fileno.return_value = 100
        process.poll.side_effect = AssertionError('early reap forbidden')
        self.waitid.side_effect = None
        self.waitid.return_value = SimpleNamespace(si_pid=424242, si_code=guest.os.CLD_EXITED, si_status=60)
        pipes = {99: iter([b'20 000\n', b'']), 100: iter([b'PRIVATE CERT RESPONSE', b''])}
        with patch.object(guest.subprocess, 'Popen', return_value=process) as popen, \
             patch.object(guest, 'stop') as stop, \
             patch.object(guest.select, 'select', side_effect=lambda fds, *a: (list(fds), [], [])), \
             patch.object(guest.os, 'read', side_effect=lambda fd, size: next(pipes[fd])), \
             patch.object(guest.sys, 'stdout', new_callable=io.StringIO) as output:
            self.assertEqual(guest.command(['/usr/bin/curl'], env={'LC_ALL': 'C.UTF-8'},
                                           timeout=12, merge=False), (60, b'20 000\n'))
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        self.assertEqual(popen.call_args.kwargs['stdin'], guest.subprocess.DEVNULL)
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('PRIVATE', json.dumps(guest.COMMAND_FACTS))
        stop.assert_called_once_with(424242)
        process.wait.assert_called_once_with(timeout=3)
        process.stdout.close.assert_called_once()
        process.stderr.close.assert_called_once()


class DaemonCATests(Isolated):
    def setUp(self):
        super().setUp()
        self.env = guest.environment(transport='guest-fetch')
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))
        self.opened = self.enterContext(patch.object(guest.os, 'open', side_effect=range(100, 106)))
        self.closed = self.enterContext(patch.object(guest.os, 'close'))
        self.mkdir = self.enterContext(patch.object(guest.os, 'mkdir'))
        self.metadata = self.enterContext(patch.object(guest.os, 'fstat', side_effect=[
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0) for _ in range(4)] + [
            SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0),
            SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)]))
        self.stream = Mock()
        self.stream.fileno.return_value = 105
        self.fdopen = self.enterContext(patch.object(guest.os, 'fdopen'))
        self.fdopen.return_value.__enter__.return_value = self.stream
        self.fsync = self.enterContext(patch.object(guest.os, 'fsync'))
        self.checked = self.enterContext(patch.object(guest, 'checked', side_effect=[b'', b'', b'active\n']))

    def test_exclusive_fixed_runtime_file_and_bounded_service_commands(self):
        guest.configure_daemon_ca(self.env)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self.assertEqual([(c.args, c.kwargs) for c in self.opened.call_args_list], [
            (('/', flags), {}), (('run', flags), {'dir_fd': 100}),
            (('systemd', flags), {'dir_fd': 101}), (('system', flags), {'dir_fd': 102}),
            (('nix-daemon.service.d', flags), {'dir_fd': 103}),
            (('envy-runtime-ca.conf', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), {'dir_fd': 104})])
        self.mkdir.assert_called_once_with('nix-daemon.service.d', mode=0o700, dir_fd=103)
        self.fdopen.assert_called_once_with(105, 'wb')
        self.stream.write.assert_called_once_with(
            b'[Service]\nEnvironment=NIX_SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt\n')
        self.stream.flush.assert_called_once_with()
        self.fsync.assert_called_once_with(105)
        self.assertEqual([c.args for c in self.closed.call_args_list], [(104,), (103,), (102,), (101,), (100,)])
        self.assertEqual([(c.args[0], c.kwargs) for c in self.checked.call_args_list], [
            (['/usr/bin/systemctl', 'daemon-reload'], {'env': self.env, 'timeout': 15}),
            (['/usr/bin/systemctl', 'restart', 'nix-daemon.service'], {'env': self.env, 'timeout': 30}),
            (['/usr/bin/systemctl', 'is-active', 'nix-daemon.service'], {'env': self.env, 'timeout': 10})])

    def test_each_parent_and_new_directory_metadata_refuses_without_systemd(self):
        for index in range(5):
            for mode, uid in ((stat.S_IFLNK | 0o777, 0), (stat.S_IFREG | 0o644, 0),
                              (stat.S_IFDIR | 0o755, 1000), (stat.S_IFDIR | 0o775, 0),
                              (stat.S_IFDIR | 0o757, 0)):
                with self.subTest(index=index, mode=mode, uid=uid):
                    self.opened.side_effect = range(100, 106)
                    self.metadata.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
                                                for _ in range(index)] + [SimpleNamespace(st_mode=mode, st_uid=uid)]
                    with self.assertRaises(guest.GuestFailure):
                        guest.configure_daemon_ca(self.env)
                    self.checked.assert_not_called()
                    self.stream.write.assert_not_called()

    def test_foreign_directory_and_exclusive_file_collisions_never_overwrite(self):
        for boundary in ('mkdir', 'open-parent', 'open-dropin', 'open-file'):
            with self.subTest(boundary=boundary), ExitStack() as stack:
                self.opened.side_effect = range(100, 106)
                self.metadata.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
                                            for _ in range(4)] + [SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0)]
                if boundary == 'mkdir':
                    stack.enter_context(patch.object(guest.os, 'mkdir', side_effect=FileExistsError('PRIVATE')))
                else:
                    index = {'open-parent': 1, 'open-dropin': 4, 'open-file': 5}[boundary]
                    self.opened.side_effect = list(range(100, 100 + index)) + [OSError(errno.ELOOP, 'PRIVATE')]
                with self.assertRaises(OSError):
                    guest.configure_daemon_ca(self.env)
                self.checked.assert_not_called()
                self.stream.write.assert_not_called()

    def test_file_metadata_refuses_links_owner_type_and_permissions(self):
        for mode, uid, links in ((stat.S_IFREG | 0o600, 0, 2), (stat.S_IFREG | 0o600, 1000, 1),
                                 (stat.S_IFLNK | 0o600, 0, 1), (stat.S_IFREG | 0o644, 0, 1)):
            with self.subTest(mode=mode, uid=uid, links=links):
                self.opened.side_effect = range(100, 106)
                self.metadata.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
                                            for _ in range(4)] + [
                    SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0),
                    SimpleNamespace(st_mode=mode, st_uid=uid, st_nlink=links)]
                with self.assertRaises(guest.GuestFailure):
                    guest.configure_daemon_ca(self.env)
                self.checked.assert_not_called()
                self.stream.write.assert_not_called()

    def test_uid_offline_and_deadline_refuse_before_new_io(self):
        cases = ((1000, self.env, 1000), (0, guest.environment(), 1000), (0, self.env, 0))
        for uid, env, deadline in cases:
            with self.subTest(uid=uid, env=env, deadline=deadline), \
                 patch.object(guest.os, 'getuid', return_value=uid), patch.object(guest, 'DEADLINE', deadline), \
                 self.assertRaises(guest.GuestFailure):
                guest.configure_daemon_ca(env)
            self.opened.assert_not_called()
            self.mkdir.assert_not_called()
            self.checked.assert_not_called()

    def test_systemd_failure_timeout_and_cancel_stop_later_commands(self):
        for index in range(3):
            for error in (guest.GuestFailure('command-failed'), guest.GuestFailure('command-timeout'), KeyboardInterrupt()):
                self.opened.side_effect = range(100, 106)
                self.metadata.side_effect = [SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_uid=0)
                                            for _ in range(4)] + [
                    SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=0),
                    SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=0, st_nlink=1)]
                self.checked.reset_mock()
                self.checked.side_effect = [b''] * index + [error]
                with self.subTest(index=index, error=type(error)), self.assertRaises(type(error)):
                    guest.configure_daemon_ca(self.env)
                self.assertEqual(self.checked.call_count, index + 1)
                self.assertEqual(guest.DEADLINE, 1000)

    def test_install_and_daemon_failures_stop_network_and_keep_root_poweroff(self):
        for failed in ('install', 'daemon'):
            for error in (guest.GuestFailure('command-failed'), guest.GuestFailure('command-timeout'), KeyboardInterrupt()):
                self.serial.reset_mock()
                self.checked.reset_mock()
                self.checked.side_effect = None
                self.checked.return_value = b''
                with self.subTest(failed=failed, error=type(error)), ExitStack() as stack:
                    def stub(name, **kwargs):
                        return stack.enter_context(patch.object(guest, name, **kwargs))
                    stack.enter_context(patch.object(guest.sys, 'argv', [guest.SCRIPT]))
                    stack.enter_context(patch.object(guest.signal, 'signal'))
                    stub('guard')
                    stub('system_ca_metadata', return_value={
                        'regularFile': True, 'rootOwned': True, 'notGroupOtherWritable': True})
                    stack.enter_context(patch.object(guest.os.path, 'lexists', return_value=False))
                    stack.enter_context(patch.object(Path, 'mkdir'))
                    stub('payload_fd', side_effect=[10, 11])
                    stack.enter_context(patch.object(guest.os, 'read', return_value=json.dumps(network_metadata()).encode()))
                    stack.enter_context(patch.object(guest.os, 'dup', return_value=12))
                    stack.enter_context(patch.object(guest.os, 'fdopen', return_value=io.BytesIO(b'tar')))
                    stub('source_members', return_value={})
                    stub('extract_source', return_value=Path('/synthetic/source'))
                    install = stub('install_official', side_effect=error if failed == 'install' else None,
                                   return_value={'synthetic': True})
                    daemon = stub('configure_daemon_ca', side_effect=error if failed == 'daemon' else None)
                    tls = stub('public_tls_probe')
                    native = stub('native_tls_probe')
                    fetch = stub('fetch_payload')
                    cleanup = stub('stop_user_unit')
                    self.assertIsNone(guest.main(network_config()))
                install.assert_called_once()
                self.assertEqual(daemon.call_count, int(failed == 'daemon'))
                for mocked in (tls, native, fetch, cleanup):
                    mocked.assert_not_called()
                self.assertEqual(self.checked.call_count, 2)
                self.assertEqual(self.checked.call_args.args[0], ['/usr/bin/systemctl', 'poweroff', '--no-block'])
                self.assertEqual(self.serial.call_args.args[0], guest.DIAGNOSTIC_PREFIX)
                row = self.serial.call_args.args[1]
                self.assertEqual(row['phase'], 'official-install' if failed == 'install' else 'daemon-ca')
                self.assertEqual(row['error'], 'cancelled' if isinstance(error, KeyboardInterrupt) else error.args[0])
                self.assertNotIn(guest.PREFIX, [c.args[0] for c in self.serial.call_args_list])

    def test_nonactive_stdout_does_not_count_as_active(self):
        self.checked.side_effect = [b'', b'', b'PRIVATE inactive\n']
        with self.assertRaisesRegex(guest.GuestFailure, '^daemon-inactive$'):
            guest.configure_daemon_ca(self.env)
        self.serial.assert_not_called()


class NativeTLSTests(Isolated):
    def setUp(self):
        super().setUp()
        self.env = guest.environment(transport='guest-fetch')
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))

    def test_single_setting_and_cache_argv_use_existing_nix_wrapper(self):
        with patch.object(guest, 'checked', side_effect=[(guest.SYSTEM_CA_BUNDLE + '\n').encode(), b'PRIVATE']) as checked:
            guest.native_tls_probe(network_config(), self.env)
        base = [guest.NIX, '--extra-experimental-features', 'nix-command flakes', '--option', 'builders', '']
        self.assertEqual([c.args[0] for c in checked.call_args_list], [
            base + ['config', 'show', 'ssl-cert-file'],
            base + ['store', 'ping', '--store', 'https://cache.nixos.org', '--json']])
        self.assertTrue(all(c.kwargs == {'env': self.env, 'merge': False, 'timeout': 15}
                            for c in checked.call_args_list))
        rows = [c.args[1] for c in self.serial.call_args_list]
        self.assertEqual(rows, [
            {'schemaVersion': 1, 'token': TOKEN, 'endpoint': 'nix-ca-setting', 'exitCode': 0,
             'settingMatchesSystemBundle': True, 'error': None},
            {'schemaVersion': 1, 'token': TOKEN, 'endpoint': 'nix-cache-ping', 'exitCode': 0,
             'settingMatchesSystemBundle': None, 'error': None}])
        self.assertTrue(all(c.args[0] == guest.NATIVE_TLS_PREFIX for c in self.serial.call_args_list))
        self.assertNotIn('PRIVATE', json.dumps(rows))
        self.assertNotIn(guest.SYSTEM_CA_BUNDLE, json.dumps(rows))

    def test_setting_mismatch_and_cache_failure_report_only_closed_facts(self):
        def command(argv, **kwargs):
            code = 0 if 'config' in argv else 60
            guest.COMMAND_FACTS.append({'exitCode': code, 'outputBytes': 13, 'outputSha256': 'a' * 64})
            return code, b'PRIVATE token'
        with patch.object(guest, 'command', side_effect=command):
            guest.native_tls_probe(network_config(), self.env)
        rows = [c.args[1] for c in self.serial.call_args_list]
        self.assertIs(rows[0]['settingMatchesSystemBundle'], False)
        self.assertEqual(rows[1]['exitCode'], 60)
        self.assertEqual(rows[1]['error'], 'command-failed')
        self.assertNotIn('PRIVATE', json.dumps(rows))

    def test_timeout_cancel_and_serial_failure_propagate_without_retry(self):
        for error in (guest.GuestFailure('command-timeout'), guest.GuestFailure('timeout'), KeyboardInterrupt()):
            with patch.object(guest, 'nix', side_effect=error) as nix, self.assertRaises(type(error)):
                guest.native_tls_probe(network_config(), self.env)
            self.assertEqual(nix.call_count, 1)
        self.serial.assert_not_called()
        self.serial.side_effect = OSError('PRIVATE')
        with patch.object(guest, 'nix', return_value=b'PRIVATE') as nix, self.assertRaises(OSError):
            guest.native_tls_probe(network_config(), self.env)
        self.assertEqual(nix.call_count, 1)

    def test_user_and_offline_have_zero_probe_effects(self):
        for uid, config, env in ((1000, network_config(), self.env), (0, CONFIG, guest.environment())):
            with patch.object(guest.os, 'getuid', return_value=uid), patch.object(guest, 'nix') as nix, \
                 self.assertRaises(guest.GuestFailure):
                guest.native_tls_probe(config, env)
            nix.assert_not_called()
        self.serial.assert_not_called()




class PrefetchObservationTests(unittest.TestCase):
    URL = 'https://codeload.github.com/NixOS/nixpkgs/tar.gz/' + 'a' * 40

    @classmethod
    def start(cls, ident=1, stage='download', **changes):
        row = {'action': 'start', 'id': ident, 'level': 4, 'parent': 0,
               'type': 101 if stage == 'download' else 0,
               'text': {'download': "downloading '" + cls.URL + "'",
                        'unpack': "unpacking '" + cls.URL + "'",
                        'store-add': "adding '" + cls.URL + "' to the store"}[stage]}
        if stage == 'download':
            row['fields'] = [cls.URL]
        return {**row, **changes}

    @staticmethod
    def wire(row):
        return b'@nix ' + json.dumps(row, ensure_ascii=False).encode() + b'\n'

    def observer(self, *rows):
        observer = guest._PrefetchObservation(self.URL)
        for row in rows:
            observer.feed(self.wire(row))
        return observer

    def code(self, observer, *, exited=False, stdout=False, stderr=True):
        return observer.timeout_code(exited, stdout, stderr)

    def test_native_stages_all_splits_and_bytewise_utf8(self):
        for stage in ('download', 'unpack', 'store-add'):
            raw = self.wire(self.start(stage=stage))
            for split in range(len(raw) + 1):
                observer = guest._PrefetchObservation(self.URL)
                observer.feed(raw[:split])
                observer.feed(raw[split:])
                self.assertEqual(self.code(observer), 'prefetch-timeout-active-' + stage)
            raw = self.wire(self.start(text='下载 🌍')) if stage == 'download' else raw
            observer = guest._PrefetchObservation(self.URL)
            for byte in raw:
                observer.feed(bytes([byte]))
            self.assertEqual(self.code(observer), 'prefetch-timeout-active-' + stage)
            self.assertEqual(len(observer.pending), 0)

    def test_official_omitted_fields_and_explicit_empty_fields(self):
        for stage in ('unpack', 'store-add'):
            for change in ({}, {'fields': []}):
                self.assertEqual(self.code(self.observer(self.start(stage=stage, **change))),
                                 'prefetch-timeout-active-' + stage)

    def test_overlap_not_last_line_and_stop_closes_only_known_id(self):
        observer = self.observer(self.start(), self.start(2, 'unpack'))
        self.assertEqual(self.code(observer), 'prefetch-timeout-overlap')
        observer.feed(self.wire({'action': 'stop', 'id': 2}))
        self.assertEqual(self.code(observer), 'prefetch-timeout-active-download')
        observer.feed(self.wire({'action': 'stop', 'id': 1}))
        self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')

    def test_stop_and_hundred_percent_do_not_authorize_success(self):
        observer = self.observer(self.start(), {'action': 'result', 'id': 1, 'type': 105,
                                               'fields': [100, 100, 0, 0]})
        self.assertEqual(self.code(observer), 'prefetch-timeout-active-download')
        observer.feed(self.wire({'action': 'stop', 'id': 1}))
        self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')

    def test_other_known_native_activity_and_result_are_ignored_without_text_inference(self):
        observer = self.observer(self.start(1, type=100, fields=['PRIVATE'], text='PRIVATE'),
                                 {'action': 'result', 'id': 1, 'type': 101, 'fields': ['PRIVATE']},
                                 self.start(2, 'unpack'))
        self.assertEqual(self.code(observer), 'prefetch-timeout-active-unpack')
        observer.feed(self.wire({'action': 'stop', 'id': 1}))
        self.assertEqual(self.code(observer), 'prefetch-timeout-active-unpack')

    def test_msg_and_plain_text_never_supply_activity(self):
        for raw in (self.wire({'action': 'msg', 'level': 0, 'msg': self.start()['text']}),
                    (self.start()['text'] + '\n').encode()):
            observer = guest._PrefetchObservation(self.URL)
            observer.feed(raw)
            self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')
            observer.feed(self.wire(self.start()))
            self.assertEqual(self.code(observer), 'prefetch-timeout-active-download')

    def test_bad_msg_schema_then_legal_activity_permanently_poison(self):
        valid = {'action': 'msg', 'level': 0, 'msg': 'PRIVATE'}
        rows = [{**valid, 'level': level} for level in (-1, 8, True, False, 0.0, '0', None, 2**64)]
        rows += [{**valid, 'msg': value} for value in (None, 0, True, [], {})]
        rows += [{**valid, key: 'PRIVATE'} for key in ('extra', 'raw_msg', 'trace', 'fields')]
        rows += [{key: value for key, value in valid.items() if key != missing}
                 for missing in ('action', 'level', 'msg')]
        for row in rows:
            with self.subTest(row=row):
                observer = self.observer(row, self.start())
                self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')
                self.assertTrue(observer.unknown)
                self.assertEqual(len(observer.pending), 0)

    def test_correct_msg_after_poison_cannot_restore_activity(self):
        observer = self.observer(self.start(), {'action': 'msg', 'level': 0, 'msg': 'PRIVATE', 'extra': 0},
                                 {'action': 'msg', 'level': 0, 'msg': self.start(stage='unpack')['text']},
                                 self.start(2, 'unpack'))
        self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')
        self.assertTrue(observer.unknown)

    def test_basic_official_msg_levels_ignore_text_and_preserve_real_activity(self):
        for level in range(8):
            for text in (self.start()['text'], self.start(stage='unpack')['text'],
                         json.dumps(self.start()), 'PRIVATE 🌍'):
                with self.subTest(level=level, text=text):
                    row = {'action': 'msg', 'level': level, 'msg': text}
                    observer = self.observer(row)
                    self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')
                    self.assertFalse(observer.unknown)
                    self.assertEqual(observer.active, {})
                    observer.feed(self.wire(self.start()))
                    observer.feed(self.wire(row))
                    self.assertEqual(self.code(observer), 'prefetch-timeout-active-download')

    def test_msg_poison_keeps_independent_exit_observed_open_stream_priority(self):
        observer = self.observer({'action': 'msg', 'level': True, 'msg': 'PRIVATE'}, self.start())
        self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')
        for stdout, stderr in ((True, False), (False, True), (True, True)):
            self.assertEqual(self.code(observer, exited=True, stdout=stdout, stderr=stderr),
                             'prefetch-timeout-exit-observed-open-stream')
        self.assertEqual(self.code(observer, exited=True, stdout=False, stderr=False),
                         'prefetch-timeout-unknown')

    def test_bad_schema_unknown_actions_fields_and_types_permanently_poison(self):
        rows = [self.start(extra='PRIVATE'), self.start(id=True), self.start(id=-1),
                self.start(id=2**64), self.start(id=1.0), self.start(parent=True),
                self.start(parent=2**64), self.start(level=True), self.start(type=True),
                self.start(type=999), self.start(text=1), self.start(fields=[True]),
                self.start(fields='PRIVATE'), self.start(fields=[self.URL, self.URL]),
                {'action': 'stop', 'id': 1, 'extra': 0}, {'action': 'stop', 'id': True},
                {'action': 'stop', 'id': 17}, {'action': 'unknown'},
                {'action': 'result', 'id': 1, 'type': True},
                {'action': 'result', 'id': 1, 'type': 105, 'fields': [False]},
                {'action': 'result', 'id': 1, 'type': 999},
                {'action': 'result', 'id': 99, 'type': 105, 'fields': []},
                {'action': 'result', 'id': 1, 'type': 105, 'fields': [], 'extra': 0}]
        missing = self.start()
        del missing['text']
        rows.append(missing)
        for row in rows:
            with self.subTest(row=row):
                observer = self.observer(self.start(7), row, self.start(8, 'unpack'))
                self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')

    def test_bad_json_duplicates_nonfinite_encoding_and_nonobject_poison(self):
        for raw in (b'@nix {bad}\n', b'@nix {"action":"stop","id":1,"id":1}\n',
                    b'@nix {"action":"msg","msg":NaN}\n', b'@nix []\n',
                    b'@nix {"action":"msg","msg":"\xff"}\n',
                    b'@nix ' + b'[' * 1500 + b']' * 1500 + b'\n'):
            observer = self.observer(self.start())
            observer.feed(raw)
            observer.feed(self.wire(self.start(2, 'unpack')))
            self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')

    def test_wrong_url_and_changed_phase_text_poison(self):
        for row in (self.start(fields=[self.URL + '?PRIVATE']),
                    self.start(stage='unpack', text="unpacking 'PRIVATE'"),
                    self.start(stage='store-add', text="adding 'PRIVATE' to the store"),
                    self.start(stage='unpack', fields=[self.URL])):
            observer = self.observer(row, self.start(2))
            self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')

    def test_stale_stop_and_stopped_id_reuse_and_seventeenth_cumulative_id_poison(self):
        rows = [self.start(), {'action': 'stop', 'id': 1}]
        for ending in ({'action': 'stop', 'id': 1}, self.start(1)):
            self.assertEqual(self.code(self.observer(*rows, ending, self.start(2))),
                             'prefetch-timeout-unknown')
        observer = guest._PrefetchObservation(self.URL)
        for ident in range(1, 17):
            observer.feed(self.wire(self.start(ident)))
            self.assertEqual(self.code(observer), 'prefetch-timeout-active-download')
            observer.feed(self.wire({'action': 'stop', 'id': ident}))
        observer.feed(self.wire(self.start(17)))
        self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')
        self.assertLessEqual(len(observer.seen), 16)

    def test_uint64_id_upper_boundary_is_valid(self):
        observer = self.observer(self.start(2**64 - 1, parent=2**64 - 1))
        self.assertEqual(self.code(observer), 'prefetch-timeout-active-download')
        observer.feed(self.wire({'action': 'stop', 'id': 2**64 - 1}))
        self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')

    def test_raw_line_cap_and_incomplete_tail_are_permanent(self):
        for chunks in ([b'x' * 4097 + b'\n'], [b'x'] * 4097 + [b'\n'],
                       [self.wire(self.start())[:-1]], [b'@nix {"text":"\xe4']):
            observer = self.observer(self.start(7))
            for raw in chunks:
                observer.feed(raw)
                self.assertLessEqual(len(observer.pending), 4096)
            observer.finish()
            observer.feed(self.wire(self.start(8)))
            self.assertEqual(self.code(observer), 'prefetch-timeout-unknown')
        observer = self.observer(self.start())
        observer.feed(b'x' * 4096 + b'\n')
        self.assertEqual(self.code(observer), 'prefetch-timeout-active-download')
        self.assertEqual(len(observer.pending), 0)

    def test_exit_observed_open_stream_has_priority_even_when_poisoned(self):
        for observer in (self.observer(self.start()), self.observer(self.start(), self.start(2, 'unpack')),
                         self.observer({'action': 'stop', 'id': 99})):
            for stdout, stderr in ((True, False), (False, True), (True, True)):
                self.assertEqual(self.code(observer, exited=True, stdout=stdout, stderr=stderr),
                                 'prefetch-timeout-exit-observed-open-stream')
        self.assertEqual(self.code(self.observer(self.start()), exited=True, stdout=False, stderr=False),
                         'prefetch-timeout-active-download')

    def test_static_timeout_codes_fit_existing_root_codec_without_new_fields(self):
        for suffix in ('active-download', 'active-unpack', 'active-store-add', 'overlap',
                       'exit-observed-open-stream', 'unknown'):
            code = 'prefetch-timeout-' + suffix
            self.assertIn(code, guest.DIAGNOSTIC_CODES)
            self.assertRegex(code, r'^[a-z0-9:-]{1,100}$')
            with patch.object(guest, 'emit_serial') as serial:
                guest.emit_diagnostic(CONFIG, guest.GuestFailure(code))
            row = serial.call_args.args[1]
            self.assertEqual(row['error'], code)
            self.assertEqual(set(row), {'schemaVersion', 'token', 'phase', 'error', 'lastCommands'})


class PrefetchCommandTests(CommandOwnershipTests):
    # Inherit the existing ownership guards to exercise the same command path.
    def setup_prefetch(self, rows=(), *, observed=False, cleanup_error=None):
        self.boundaries(observations=[SimpleNamespace(si_pid=424242, si_code=1, si_status=0)
                                      if observed else None], cleanup_error=cleanup_error)
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(guest, 'ROOT_PHASE', 'input-prefetch'))
        raw = b''.join(PrefetchObservationTests.wire(row) for row in rows)
        self.chunks = {99: iter([b'{}']), 100: iter([raw or b'plain\n'])}
        self.enterContext(patch.object(guest, 'tick', side_effect=[None, None, guest.GuestFailure('timeout')]))
        self.options['_prefetch_url'] = PrefetchObservationTests.URL

    def test_prefetch_timeout_mapping_freezes_before_cleanup_and_keeps_no_fact(self):
        self.setup_prefetch([PrefetchObservationTests.start()])
        with self.assertRaisesRegex(guest.GuestFailure, '^prefetch-timeout-active-download$'):
            guest.command([guest.NIX], **self.options)
        self.assert_cleanup(False)
        self.assertEqual(guest.COMMAND_FACTS, [])

    def test_prefetch_exit_observed_wins_before_cleanup_changes_leader_state(self):
        self.setup_prefetch([PrefetchObservationTests.start()], observed=True, cleanup_error='stop')
        with self.assertRaisesRegex(guest.GuestFailure, '^prefetch-timeout-exit-observed-open-stream$'):
            guest.command([guest.NIX], **self.options)
        self.assertEqual(self.events, ['observe', 'stop', 'reap', 'stdout-close', 'stderr-close'])

    def test_non_timeout_first_error_and_all_cleanup_survive_prefetch_observation(self):
        self.setup_prefetch([PrefetchObservationTests.start()], cleanup_error='stop')
        error = guest.GuestFailure('first-error')
        with self.assertRaises(guest.GuestFailure) as caught:
            guest.command([guest.NIX], dialogue=SimpleNamespace(feed=Mock(side_effect=error)), **self.options)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, ['stop', 'reap', 'stdout-close', 'stderr-close'])

    def test_prefetch_classifier_cap_is_four_kib_other_commands_keep_eight(self):
        # A classifier match past 4096 is retained only outside observed prefetch.
        for enabled, expected in ((True, 'unknown'), (False, 'nix-dns')):
            with self.subTest(enabled=enabled), ExitStack() as stack:
                stack.enter_context(patch.object(guest, 'tick'))
                self.boundaries()
                stack.enter_context(patch.object(guest.os, 'getuid', return_value=0))
                stack.enter_context(patch.object(guest.os, 'geteuid', return_value=0))
                stack.enter_context(patch.object(guest, 'ROOT_PHASE', 'input-prefetch'))
                self.chunks = {99: iter([b'{}', b'']),
                               100: iter([b'x' * 4096 + b'Could not resolve host\n', b''])}
                self.waitid.side_effect = None
                self.waitid.return_value = SimpleNamespace(si_pid=424242, si_code=1, si_status=1)
                kwargs = {'_prefetch_url': PrefetchObservationTests.URL} if enabled else {}
                self.assertEqual(guest.command([guest.NIX], **self.options, **kwargs), (1, b'{}'))
                self.assertEqual(guest.LAST_NIX_FAILURE, expected)
                self.assert_cleanup(False)

    def test_prefetch_success_stdout_and_receipt_are_unchanged_even_bad_activity(self):
        self.boundaries()
        self.enterContext(patch.object(guest.os, 'getuid', return_value=0))
        self.enterContext(patch.object(guest.os, 'geteuid', return_value=0))
        self.enterContext(patch.object(guest, 'ROOT_PHASE', 'input-prefetch'))
        self.chunks = {99: iter([b'{"storePath":"PRIVATE"}', b'']), 100: iter([b'@nix {bad}\n', b''])}
        self.assertEqual(guest.command([guest.NIX], _prefetch_url=PrefetchObservationTests.URL,
                                      **self.options), (0, b'{"storePath":"PRIVATE"}'))
        self.assertEqual(set(guest.COMMAND_FACTS[-1]), {'exitCode', 'outputBytes', 'outputSha256'})
        self.assert_cleanup(False)

    def test_prefetch_output_limit_and_echild_keep_original_error_and_authority(self):
        self.setup_prefetch([PrefetchObservationTests.start()])
        with patch.object(guest, 'MAX_OUTPUT', 1), self.assertRaisesRegex(guest.GuestFailure, '^command-output-limit$'):
            guest.command([guest.NIX], **self.options)
        self.assert_cleanup(False)
        self.setup_prefetch([PrefetchObservationTests.start()])
        error = ChildProcessError(errno.ECHILD, 'synthetic')
        self.waitid.side_effect = error
        def reap_missing(**kwargs):
            self.events.append('reap')
            raise ChildProcessError(errno.ECHILD, 'still absent')
        self.popen.return_value.wait = Mock(side_effect=reap_missing)
        with self.assertRaises(ChildProcessError) as caught:
            guest.command([guest.NIX], **self.options)
        self.assertIs(caught.exception, error)
        self.assertEqual(self.events, ['reap', 'stdout-close', 'stderr-close'])

    def test_command_timeout_and_unobserved_exit_are_not_inferred_from_cleanup(self):
        self.setup_prefetch([PrefetchObservationTests.start()])
        with patch.object(guest, 'tick'), patch.object(guest.time, 'monotonic', side_effect=[1, 1, 1001]):
            with self.assertRaisesRegex(guest.GuestFailure, '^prefetch-timeout-active-download$'):
                guest.command([guest.NIX], **self.options)
        self.assert_cleanup(False)


class PrefetchNativePipeTests(unittest.TestCase):
    def test_real_wnowait_exited_leader_with_descendant_holding_both_pipes(self):
        import subprocess
        import time
        events, processes = [], []
        original_popen, original_waitid, original_stop = subprocess.Popen, os.waitid, guest.stop
        def spawn(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            original_wait = process.wait
            def wait(**options):
                events.append('reap')
                return original_wait(**options)
            process.wait = wait
            return process
        def observe(*args):
            result = original_waitid(*args)
            if result is not None:
                events.append('observed-exit')
            return result
        def stop(pid):
            self.assertIn('observed-exit', events)
            self.assertNotIn('reap', events)
            events.append('stop')
            original_stop(pid)
        raw = PrefetchObservationTests.wire(PrefetchObservationTests.start())
        body = ('import os,time\n'
                'child=os.fork()\n'
                'if child == 0:\n'
                ' time.sleep(30)\n'
                ' os._exit(0)\n'
                f'os.write(2,{raw!r})\n'
                'os._exit(0)\n')
        with patch.object(guest.subprocess, 'Popen', side_effect=spawn), \
             patch.object(guest.os, 'waitid', side_effect=observe), patch.object(guest, 'stop', side_effect=stop), \
             patch.object(guest, 'DEADLINE', time.monotonic() + 5), \
             patch.object(guest, 'ROOT_PHASE', 'input-prefetch'), patch.object(guest, 'COMMAND_FACTS', []), \
             patch.object(guest, 'NIX', sys.executable), patch.object(guest.os, 'getuid', return_value=0), \
             patch.object(guest.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(guest.GuestFailure, '^prefetch-timeout-exit-observed-open-stream$'):
                guest.command([sys.executable, '-c', body], env={}, merge=False, timeout=1,
                              _prefetch_url=PrefetchObservationTests.URL)
            self.assertEqual(guest.COMMAND_FACTS, [])
        self.assertEqual(events.count('observed-exit'), 1)
        self.assertEqual(events[-2:], ['stop', 'reap'])
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        with self.assertRaises(ChildProcessError):
            original_waitid(os.P_PID, processes[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


    def test_fixed_source_calculator_uses_owned_command_timeout_and_lock_finally(self):
        import subprocess
        import time
        events, processes = [], []
        original_popen, original_waitid, original_stop = subprocess.Popen, os.waitid, guest.stop
        def spawn(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            original_wait = process.wait
            def wait(**options):
                events.append('reap')
                return original_wait(**options)
            process.wait = wait
            return process
        def observe(*args):
            result = original_waitid(*args)
            if result is not None:
                events.append('observed-exit')
            return result
        def stop(pid):
            self.assertIn('observed-exit', events)
            self.assertNotIn('reap', events)
            events.append('stop')
            original_stop(pid)
        with tempfile.TemporaryDirectory(prefix='envy-cache-calculator-') as directory:
            root = Path(directory)
            fixture, source = root / 'fixture', root / 'source'
            raw = b'private synthetic lock bytes'
            for path in (fixture, source):
                path.mkdir(mode=0o700)
                (path / 'flake.lock').write_bytes(raw)
                (path / 'flake.lock').chmod(0o600)
            script = root / 'nix-store'
            script.write_text('#!' + sys.executable + '\nimport os,time\n'
                              'child=os.fork()\nif child == 0:\n time.sleep(30)\n os._exit(0)\n'
                              'os.write(1,b"/nix/store/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa-source\\n")\n'
                              'os._exit(0)\n')
            script.chmod(0o700)
            original_read = guest.private_read
            reads = []
            def read(path, *args, **kwargs):
                reads.append(path)
                return original_read(path, *args, **kwargs)
            with patch.object(guest.subprocess, 'Popen', side_effect=spawn), \
                 patch.object(guest.os, 'waitid', side_effect=observe), patch.object(guest, 'stop', side_effect=stop), \
                 patch.object(guest, 'private_read', side_effect=read), patch.object(guest, 'STORE', str(script)), \
                 patch.object(guest, 'DEADLINE', time.monotonic() + 1), patch.object(guest, 'COMMAND_FACTS', []):
                with self.assertRaisesRegex(guest.GuestFailure, '^(timeout|command-timeout)$'):
                    guest._locked_prefetch_native(
                        ['--print-fixed-path', '--recursive', 'sha256', sri(b'input'), 'source'],
                        {'ENVY_RUNTIME_LOCK_SHA256': guest.sha(raw)}, fixture, source, fixed=True)
                self.assertEqual(guest.COMMAND_FACTS, [])
            self.assertEqual(reads, [fixture / 'flake.lock', source / 'flake.lock'] * 2)
        self.assertEqual(events.count('observed-exit'), 1)
        self.assertEqual(events[-2:], ['stop', 'reap'])
        self.assertEqual(processes[0].returncode, 0)
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        with self.assertRaises(ChildProcessError):
            original_waitid(os.P_PID, processes[0].pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)


class CacheCopyObservationTests(unittest.TestCase):
    PATH = '/nix/store/' + 'c' * 32 + '-source'

    @classmethod
    def start(cls, ident=1, **changes):
        return {'action': 'start', 'id': ident, 'level': 4, 'type': 100,
                'text': '复制 🌍', 'parent': 0,
                'fields': [cls.PATH, 'https://cache.nixos.org', 'daemon'], **changes}

    @staticmethod
    def wire(row):
        return b'@nix ' + json.dumps(row, ensure_ascii=False).encode() + b'\n'

    @staticmethod
    def progress(done=0, expected=0, ident=1, **changes):
        return {'action': 'result', 'id': ident, 'type': 105,
                'fields': [done, expected, 0, 0], **changes}

    def observer(self, *rows):
        observer = guest._CacheCopyObservation(self.PATH)
        for row in rows:
            observer.feed(self.wire(row))
        return observer

    def code(self, observer, **changes):
        return observer.timeout_code(**{'exit_observed': False, 'stdout_open': False,
                                        'stderr_open': True, **changes})

    def assert_suffix(self, observer, suffix):
        self.assertEqual(self.code(observer), 'cache-copy-timeout-' + suffix)

    def test_official_identity_all_splits_utf8_and_fixed_uri_allowlists(self):
        for source in ('https://cache.nixos.org', 'https://cache.nixos.org/'):
            for dest in ('local', 'daemon'):
                raw = self.wire(self.start(fields=[self.PATH, source, dest]))
                for split in range(len(raw) + 1):
                    observer = self.observer()
                    observer.feed(raw[:split])
                    observer.feed(raw[split:])
                    self.assert_suffix(observer, 'unclosed-no-progress')
                    self.assertFalse(observer.pending)
                observer = self.observer()
                for byte in raw:
                    observer.feed(bytes([byte]))
                self.assert_suffix(observer, 'unclosed-no-progress')

    def test_ignored_activities_omitted_fields_and_text_never_bind(self):
        for kind in (0, 101, 103, 112):
            for explicit in (False, True):
                start = self.start(type=kind, text='copy complete ' + self.PATH)
                if explicit:
                    start['fields'] = []
                else:
                    del start['fields']
                observer = self.observer(start, {'action': 'result', 'id': 1, 'type': 106},
                                         {'action': 'stop', 'id': 1},
                                         {'action': 'msg', 'level': 3, 'msg': 'import successful'})
                self.assert_suffix(observer, 'unknown')
                observer.feed(self.wire(self.start(2)))
                self.assert_suffix(observer, 'unclosed-no-progress')
        observer = self.observer(self.start(7, type=101, fields=['https://unrelated.invalid/']),
                                 self.progress(9, 10, ident=7))
        self.assert_suffix(observer, 'unknown')
        observer.feed(self.wire(self.start()))
        self.assert_suffix(observer, 'unclosed-no-progress')

    def test_copy_identity_mismatch_and_ambiguity_poison_permanently(self):
        fields = [[], [self.PATH], [self.PATH, 'https://cache.nixos.org'],
                  [self.PATH, 'https://cache.nixos.org', 'daemon', 'extra'],
                  [self.PATH + '-other', 'https://cache.nixos.org', 'daemon'],
                  [Path(self.PATH).name, 'https://cache.nixos.org', 'daemon']]
        fields += [[self.PATH, source, 'daemon'] for source in
                   ('http://cache.nixos.org', 'https://cache.nixos.org//',
                    'https://cache.nixos.org.evil', 'https://cache.nixos.org?x')]
        fields += [[self.PATH, 'https://cache.nixos.org', dest] for dest in
                   ('daemon://', 'local://', '/', 'unix:///nix/var/nix/daemon-socket/socket', True)]
        for value in fields:
            with self.subTest(fields=value):
                observer = self.observer(self.start(fields=value), self.start(2))
                self.assertTrue(observer.unknown)
                self.assert_suffix(observer, 'unknown')
        observer = self.observer(self.start(), self.start(2))
        self.assertTrue(observer.unknown)
        self.assert_suffix(observer, 'unknown')

    def test_closed_schemas_json_types_and_lifecycle_poison_permanently(self):
        invalid = [b'@nix {bad}\n', b'@nix {"action":"msg","action":"msg","level":1,"msg":"x"}\n',
                   b'@nix {"action":"msg","level":NaN,"msg":"x"}\n', b'@nix \xff\n']
        rows = [{}, [], {'action': 'other'}, {'action': True},
                {'action': 'msg', 'level': True, 'msg': 'x'},
                {'action': 'msg', 'level': 2, 'msg': 1},
                {'action': 'msg', 'level': 2, 'msg': 'x', 'extra': 0},
                {'action': 'stop', 'id': 9}, {'action': 'stop', 'id': 1, 'fields': []},
                {'action': 'result', 'id': 9, 'type': 105, 'fields': [0, 0, 0, 0]}]
        rows += [self.start(2, **change) for change in
                 ({'extra': 1}, {'parent': True}, {'parent': -1}, {'level': True}, {'level': 8},
                  {'text': []}, {'type': True}, {'type': 113}, {'fields': {}}, {'fields': [None]})]
        rows += [self.start(ident=value) for value in (True, -1, 2**64, 1.5, '1', None)]
        rows += [{'action': 'result', 'id': 1, 'type': value} for value in (True, 99, 109)]
        invalid += [self.wire(row) for row in rows]
        invalid += [self.wire(self.start()),
                    self.wire({'action': 'stop', 'id': 1}) + self.wire(self.start()),
                    self.wire({'action': 'stop', 'id': 1}) + self.wire(self.progress())]
        for raw in invalid:
            with self.subTest(raw=raw):
                observer = self.observer(self.start())
                observer.feed(raw)
                observer.feed(self.wire(self.start(3)))
                self.assertTrue(observer.unknown)
                self.assert_suffix(observer, 'unknown')

    def test_cumulative_sixteen_ids_includes_ignored_and_stopped(self):
        observer = self.observer()
        for ident in range(16):
            observer.feed(self.wire(self.start(ident, type=103, fields=[])))
            observer.feed(self.wire({'action': 'stop', 'id': ident}))
        self.assertFalse(observer.unknown)
        self.assertEqual(len(observer.seen), 16)
        observer.feed(self.wire(self.start(17)))
        self.assertTrue(observer.unknown)
        observer.feed(self.wire(self.start(18)))
        self.assertEqual(len(observer.seen), 16)
        self.assert_suffix(observer, 'unknown')

    def test_line_cap_before_append_complete_release_and_incomplete_poison(self):
        observer = self.observer(self.start())
        observer.feed(b'x' * 4096)
        self.assertEqual(len(observer.pending), 4096)
        observer.feed(b'\n')
        self.assertFalse(observer.pending)
        self.assert_suffix(observer, 'unclosed-no-progress')
        for fragments in ((b'x' * 4097,), (b'x' * 4096, b'x'), (b'@nix {',)):
            observer = self.observer(self.start())
            for fragment in fragments:
                observer.feed(fragment)
                self.assertLessEqual(len(observer.pending), 4096)
            self.assert_suffix(observer, 'unknown')
            observer.feed(self.wire(self.start(2)))
            self.assert_suffix(observer, 'unknown')
            self.assertFalse(observer.pending)

    def test_progress_scalar_invariants_and_reported_total_or_stop_never_success(self):
        observer = self.observer(self.start(), self.progress())
        self.assert_suffix(observer, 'unclosed-progress')
        for done, expected in ((0, 12), (1, 12), (12, 12), (12, 12)):
            observer.feed(self.wire(self.progress(done, expected)))
            self.assert_suffix(observer, 'unclosed-reported-total' if done == 12 else 'unclosed-progress')
        observer.feed(self.wire({'action': 'stop', 'id': 1}))
        self.assert_suffix(observer, 'stop-observed')
        self.assert_suffix(self.observer(self.start(), {'action': 'stop', 'id': 1}), 'stop-observed')
        bad_fields = [[1, 0, 0, 0], [13, 12, 0, 0], [0, 12, True, 0],
                      [0, 12, 0, False], [False, 12, 0, 0], [0, True, 0, 0],
                      [0, 12, -1, 0], [0, 12, 0, 2**64], [0, 12, 0], [], ['0', 12, 0, 0]]
        for fields in bad_fields:
            observer = self.observer(self.start(), self.progress(fields=fields), self.progress(1, 12))
            self.assertTrue(observer.unknown)
            self.assert_suffix(observer, 'unknown')
        for bad in (self.progress(0, 12), self.progress(2, 13), self.progress(0, 0)):
            observer = self.observer(self.start(), self.progress(1, 12), bad, self.progress(2, 12))
            self.assertTrue(observer.unknown)
            self.assert_suffix(observer, 'unknown')

    def test_timeout_priority_and_static_root_codec(self):
        for suffix, rows in (
                ('unknown', []), ('unclosed-no-progress', [self.start()]),
                ('unclosed-progress', [self.start(), self.progress()]),
                ('unclosed-reported-total', [self.start(), self.progress(1, 1)]),
                ('stop-observed', [self.start(), {'action': 'stop', 'id': 1}])):
            observer = self.observer(*rows)
            self.assert_suffix(observer, suffix)
            observer.feed(b'@nix {bad}\n')
            self.assert_suffix(observer, 'unknown')
            for stdout, stderr in ((True, False), (False, True), (True, True)):
                code = self.code(observer, exit_observed=True, stdout_open=stdout, stderr_open=stderr)
                self.assertEqual(code, 'cache-copy-timeout-exit-observed-open-stream')
                self.assertIn(code, guest.DIAGNOSTIC_CODES)
            self.assertEqual(self.code(observer, exit_observed=True, stdout_open=False, stderr_open=False),
                             'cache-copy-timeout-unknown')
            with patch.object(guest, 'emit_serial') as emit, patch.object(guest, 'COMMAND_FACTS', []):
                guest.emit_diagnostic(network_config(), guest.GuestFailure('cache-copy-timeout-' + suffix))
                self.assertEqual(emit.call_args.args[1]['error'], 'cache-copy-timeout-' + suffix)
                self.assertEqual(set(emit.call_args.args[1]), {'schemaVersion', 'token', 'phase', 'error', 'lastCommands'})


class CacheCopyCommandTests(unittest.TestCase):
    def run_owned(self, body, *, expected=None, timeout=0.25, output_limit=None, **options):
        import subprocess
        import time
        events, processes = [], []
        original_popen, original_waitid, original_stop = subprocess.Popen, os.waitid, guest.stop
        # Keep a private group member alive after a childless leader exits.
        # Darwin may refuse killpg on a group containing only that zombie.
        body = ('import os,time\nkeeper=os.fork()\nif keeper == 0:\n'
                ' os.close(1)\n os.close(2)\n time.sleep(30)\n os._exit(0)\n') + body
        def spawn(*args, **kwargs):
            process = original_popen(*args, **kwargs)
            processes.append(process)
            original_wait = process.wait
            def wait(**kwargs):
                events.append('reap')
                return original_wait(**kwargs)
            process.wait = wait
            return process
        def observe(*args):
            observed = original_waitid(*args)
            if observed is not None:
                events.append('exit-observed')
            return observed
        def stop(pid):
            self.assertEqual(pid, processes[0].pid)
            self.assertNotIn('reap', events)
            events.append('stop')
            original_stop(pid)
        with ExitStack() as stack:
            for obj, name, value in ((guest, 'DEADLINE', time.monotonic() + 5),
                                     (guest, 'ROOT_PHASE', 'input-prefetch'), (guest, 'NIX', sys.executable),
                                     (guest, 'COMMAND_FACTS', [])):
                stack.enter_context(patch.object(obj, name, value))
            stack.enter_context(patch.object(guest.subprocess, 'Popen', side_effect=spawn))
            stack.enter_context(patch.object(guest.os, 'waitid', side_effect=observe))
            stack.enter_context(patch.object(guest, 'stop', side_effect=stop))
            stack.enter_context(patch.object(guest.os, 'getuid', return_value=0))
            stack.enter_context(patch.object(guest.os, 'geteuid', return_value=0))
            if output_limit is not None:
                stack.enter_context(patch.object(guest, 'MAX_OUTPUT', output_limit))
            kwargs = {'_cache_copy_path': CacheCopyObservationTests.PATH, **options}
            if expected:
                with self.assertRaisesRegex(guest.GuestFailure, '^' + expected + '$'):
                    guest.command([sys.executable, '-c', body], env={}, merge=False, timeout=timeout, **kwargs)
                self.assertEqual(guest.COMMAND_FACTS, [])
                result = None
            else:
                result = guest.command([sys.executable, '-c', body], env={}, merge=False, timeout=timeout, **kwargs)
                self.assertEqual(set(guest.COMMAND_FACTS[-1]), {'exitCode', 'outputBytes', 'outputSha256'})
            classifier = guest.LAST_NIX_FAILURE
        self.assertEqual(events[-2:], ['stop', 'reap'])
        self.assertTrue(processes[0].stdout.closed)
        self.assertTrue(processes[0].stderr.closed)
        return result, events, classifier

    def test_real_exit_with_owned_descendant_open_streams_beats_poison(self):
        raw = CacheCopyObservationTests.wire(CacheCopyObservationTests.start()) + b'@nix {bad}\n'
        body = ('import os,time\nchild=os.fork()\nif child == 0:\n time.sleep(30)\n os._exit(0)\n'
                f'os.write(2,{raw!r})\nos._exit(0)\n')
        _, events, _ = self.run_owned(body, expected='cache-copy-timeout-exit-observed-open-stream', timeout=1)
        self.assertEqual(events.count('exit-observed'), 1)
        self.assertLess(events.index('exit-observed'), events.index('stop'))

    def test_unobserved_exit_is_not_running_and_stop_is_not_completion(self):
        for suffix, extra in (('unclosed-no-progress', b''),
                              ('unclosed-reported-total', CacheCopyObservationTests.wire(CacheCopyObservationTests.progress(1, 1))),
                              ('stop-observed', CacheCopyObservationTests.wire({'action': 'stop', 'id': 1}))):
            raw = CacheCopyObservationTests.wire(CacheCopyObservationTests.start()) + extra
            _, events, _ = self.run_owned(f'import os,time; os.write(2,{raw!r}); time.sleep(30)',
                                         expected='cache-copy-timeout-' + suffix)
            self.assertNotIn('exit-observed', events)

    def test_actual_eight_mib_limit_beats_poison_and_timeout_mapping(self):
        body = ('import os,time\nos.write(2,b"@nix {bad}\\n")\n'
                'for _ in range(1025):\n os.write(1,b"x"*8192)\ntime.sleep(30)\n')
        self.run_owned(body, expected='command-output-limit', timeout=4)

    def test_limit_checked_before_observation_and_success_receipt_unchanged(self):
        with patch.object(guest._CacheCopyObservation, 'feed', side_effect=AssertionError('observe after cap')):
            self.run_owned('import os,time; os.write(2,b"bad"); time.sleep(30)',
                           expected='command-output-limit', output_limit=1)
        result, _, _ = self.run_owned('import os; os.write(2,b"@nix {bad}\\n"); os.write(1,b"result")', timeout=2)
        self.assertEqual(result, (0, b'result'))

    def test_non_timeout_failure_keeps_classifier_and_copy_sample_is_four_kib(self):
        for enabled, expected in ((True, 'unknown'), (False, 'nix-dns')):
            result, _, code = self.run_owned(
                'import os; os.write(2,b"x"*4096+b"Could not resolve host\\n"); os._exit(17)', timeout=2,
                _cache_copy_path=CacheCopyObservationTests.PATH if enabled else None)
            self.assertEqual(result, (17, b''))
            self.assertEqual(code, expected)


if __name__ == '__main__':
    unittest.main()
