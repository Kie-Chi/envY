#!/usr/bin/env python3
"""Explicit disposable Linux lifecycle probe with guest-only dependency fetching."""
from __future__ import annotations

import json
import os
import secrets
import selectors
import signal
import subprocess
import sys
import time

import acceptance
import guest
import install
import vm

NetworkConfig = acceptance.NetworkConfig
NETWORK_CONFIG = b'''version: 2
ethernets:
  runtime:
    match:
      macaddress: '52:54:00:45:4e:59'
    set-name: envy0
    dhcp4: true
    dhcp6: false
    optional: true
'''
NIC = 'user,model=virtio-net-pci,mac=52:54:00:45:4e:59,ipv6=off'


def validate_config(config: NetworkConfig) -> None:
    """Reject malformed API/CLI values before any filesystem effect or spawn."""
    acceptance._validate_network_config(config)
    for name in ('workdir', 'base', 'qemu', 'qemu_img', 'mkisofs', 'nix_tar'):
        vm.safe_path(getattr(config, name))
    if not acceptance._hash(config.sha256):
        raise vm.ProbeError('invalid-sha256')


def network_argv(qemu, base_fd, overlay_fd, seed_fd):
    argv = vm.qemu_argv(qemu, base_fd, overlay_fd, seed_fd)
    indices = [i for i, item in enumerate(argv) if item == '-nic']
    if len(indices) != 1 or argv[indices[0] + 1:indices[0] + 2] != ['none']:
        raise vm.ProbeError('unexpected-qemu-network-boundary')
    argv[indices[0] + 1] = NIC
    return argv


class AbortLines:
    """Scan each stream once, retaining at most one bounded unfinished line."""
    LIMIT = 4096

    def __init__(self, token):
        self.token = token
        self.pending = bytearray()
        self.discarding = False

    def feed(self, chunk):
        parts = chunk.split(b'\n')
        for index, part in enumerate(parts):
            complete = index < len(parts) - 1
            if not self.discarding:
                if len(self.pending) + len(part) > self.LIMIT:
                    self.pending.clear()
                    self.discarding = True
                else:
                    self.pending.extend(part)
            if complete:
                if not self.discarding:
                    code = self.parse(bytes(self.pending))
                    if code is not None:
                        return code
                self.pending.clear()
                self.discarding = False
        return None

    def parse(self, line):
        # cloud-init may decorate a serial line. The bytes after the fixed
        # prefix must still be one strict JSON value, with no trailing text.
        prefix = guest.ABORT_PREFIX.encode()
        position = line.find(prefix)
        if position < 0:
            return None
        try:
            row = acceptance._json(line[position + len(prefix):])
            acceptance._keys(row, ('schemaVersion', 'token', 'error'))
            if (type(row['schemaVersion']) is int and row['schemaVersion'] == 1
                    and type(row['token']) is str and row['token'] == self.token
                    and type(row['error']) is str and row['error'] in guest.ABORT_CODES):
                return row['error']
        except (vm.ProbeError, ValueError, TypeError, KeyError, RecursionError):
            pass
        return None


class FullRunner(install.InstallRunner):
    """Keep QEMU's bounded capture even when the frozen runner would raise.

    Preparation uses InstallRunner unchanged. Serial bytes stay bound to the
    exclusively created log FD; failure handling never reopens a logfile path
    in the untrusted work namespace.
    """

    def run_qemu(self, argv, pass_fds, token):
        if not acceptance._hash(token):
            raise vm.ProbeError('invalid-guest-token')
        aborts = {True: AbortLines(token), False: AbortLines(token)}
        self.raw = bytearray()
        if time.monotonic() >= self.deadline:
            raise vm.ProbeError('timeout')
        record = {'stage': 'qemu', 'argv': argv, 'pid': None, 'returncode': None,
                  'stdout': str(self.root / 'qemu.stdout.log'),
                  'stderr': str(self.root / 'qemu.stderr.log')}
        self.commands.append(record)
        flags = vm.wait_flags()
        process, lease, selector = None, None, None
        contexts = []
        primary = None
        try:
            selector = selectors.DefaultSelector()
            streams = []
            for suffix in ('.stdout.log', '.stderr.log'):
                context = vm.exclusive_file('qemu' + suffix, directory_fd=self.directory_fd)
                streams.append(context.__enter__())
                contexts.append(context)
            stdout, stderr = streams
            process = subprocess.Popen(argv, cwd=self.cwd, env=self.env, shell=False,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True,
                                       close_fds=True, pass_fds=pass_fds)
            lease = vm._ProcessLease(process, flags, deadline=self.deadline, darwin_ps=self.darwin_ps)
            record['pid'] = process.pid
            selector.register(process.stdout, selectors.EVENT_READ, (stdout, True))
            selector.register(process.stderr, selectors.EVENT_READ, (stderr, False))
            while selector.get_map() or lease.code is None:
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    raise vm.ProbeError('timeout')
                if lease.code is None and lease.observe() is not None:
                    record['returncode'] = lease.code
                    self.stop(lease)
                for key, _ in selector.select(min(0.05, remaining)):
                    chunk = os.read(key.fileobj.fileno(), 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    allowed = max(0, self.limit - self.used)
                    bounded = chunk[:allowed]
                    key.data[0].write(bounded)
                    key.data[0].flush()
                    if key.data[1]:
                        self.raw.extend(bounded)
                    self.used += len(bounded)
                    if len(chunk) > allowed:
                        raise vm.ProbeError('output-limit')
                    aborted = aborts[key.data[1]].feed(bounded)
                    if aborted is not None:
                        raise vm.ProbeError('guest-entrypoint-failed:' + aborted)
            if lease.code != 0:
                raise vm.ProbeError('tool-failed:qemu')
        except BaseException as exc:
            primary = exc
            raise
        finally:
            cleanup_error = None
            actions = []
            if lease is not None:
                actions.extend((lambda: self.stop(lease), lease.reap))
            if process is not None:
                actions.extend(pipe.close for pipe in (process.stdout, process.stderr) if pipe is not None)
            if selector is not None:
                actions.append(selector.close)
            actions.extend(lambda context=context: context.__exit__(None, None, None)
                           for context in reversed(contexts))
            for action in actions:
                try:
                    action()
                except BaseException as exc:
                    if cleanup_error is None:
                        cleanup_error = exc
            if lease is not None:
                if lease.cleanup_evidence is not None:
                    record['cleanupEvidence'] = lease.cleanup_evidence
                if lease.code is not None:
                    record['returncode'] = lease.code
                elif lease.stopped:
                    record['returncode'] = process.returncode
            if primary is None and cleanup_error is not None:
                raise cleanup_error
        return bytes(self.raw)


def partial_facts(raw, token, source):
    """Return only bounded, identity-bound observations, never arbitrary text.

    These facts describe what the guest reported, not acceptance or rollback.
    A partial/invalid frame can never supply the success receipt or evidence.
    """
    try:
        lines = [line[len(acceptance.RECEIPT_PREFIX):] for line in raw.splitlines()
                 if line.startswith(acceptance.RECEIPT_PREFIX.encode())]
        if len(lines) != 1 or len(lines[0]) > acceptance.MAX_RECEIPT:
            return None
        row = acceptance._json(lines[0])
        acceptance._keys(row, ('schemaVersion', 'evidence', 'arch', 'token', 'source',
                               'install', 'stages', 'error', 'transport'))
        if (type(row['schemaVersion']) is not int or row['schemaVersion'] != 1
                or row['token'] != token or row['source'] != source or row['arch'] != 'x86_64'
                or row['transport'] != 'guest-fetch' or row['evidence'] != acceptance.NETWORK_EVIDENCE
                or not isinstance(row['stages'], dict) or not set(row['stages']) <= set(acceptance.STAGES)):
            return None
        facts = {'installValidated': False, 'stages': {}}
        try:
            installed = install.receipt_from(raw, token)
            facts['installValidated'] = acceptance._same(row['install'], installed)
        except vm.ProbeError:
            pass
        # Preserve only a sequential prefix. No free-form guest errors, paths,
        # command strings, journal IDs or secret values enter this projection.
        for name in acceptance.STAGES:
            stage = row['stages'].get(name)
            if not isinstance(stage, dict):
                break
            item = {}
            code = stage.get('exitCode')
            if type(code) is int and 0 <= code <= 255:
                item['exitCode'] = code
            for key in ('interactive', 'saved', 'commitDeclined', 'applyDeclined', 'existingNixBranch',
                        'tuiBuildTests', 'retryAllowed', 'originalMachineRestored', 'partialReadableAfterRecovery'):
                if type(stage.get(key)) is bool:
                    item[key] = stage[key]
            number = stage.get('generation')
            if type(number) is int and 0 < number <= 2**63 - 1:
                item['generation'] = number
            journal = stage.get('journal')
            if isinstance(journal, dict):
                observed = {}
                for key in ('result', 'terminal', 'activateStatus', 'verifyStatus'):
                    value = journal.get(key)
                    if value is None or (type(value) is str and value in ('ok', 'partial', 'error', 'unknown')):
                        observed[key] = value
                for key in ('journalPersisted', 'sourceVerified', 'conflictPartial'):
                    if type(journal.get(key)) is bool:
                        observed[key] = journal[key]
                item['journal'] = observed
            facts['stages'][name] = item
        return facts
    except (vm.ProbeError, TypeError, ValueError, KeyError):
        return None


def empty_report():
    return {'schemaVersion': 1, 'command': 'full-probe', 'status': 'unavailable', 'ok': False,
            'evidence': None, 'fullReady': False, 'transport': 'guest-fetch', 'workdir': None,
            'receipt': None, 'partial': None, 'commands': [], 'error': None, 'rollbackClaimed': False}


def full_probe(config: NetworkConfig) -> dict:
    report = empty_report()
    runner = token = origin = None
    try:
        validate_config(config)
        deadline = time.monotonic() + config.full_timeout
        with vm.preflight(config, deadline) as (base_fd, work, tools), \
                install.verified_tar(config, deadline) as archive:
            acceptance._deadline(deadline)
            work.mkdir(mode=0o700, exist_ok=False)
            report.update(status='failure', workdir=str(work))
            with vm.pinned_work(work) as (dirfd, root), \
                    vm.exclusive_file('overlay.qcow2', directory_fd=dirfd) as overlay, \
                    vm.bound_file(dirfd, 'seed.iso') as (iso_writer, iso_readonly):
                runner = FullRunner(root, tools, deadline, config.output_limit, dirfd, work,
                                    darwin_ps=config.darwin_ps)
                report['commands'] = runner.commands
                qemu, qemu_img, mkisofs = tools
                base_path = f'/dev/fd/{base_fd}'
                vm.validate_image_info(runner.run('image-info',
                    [qemu_img, 'info', '--output=json', '-f', 'qcow2', base_path],
                    capture=True, pass_fds=(base_fd,)))
                with acceptance.approved_network_payloads(config, root, deadline) as approved:
                    origin = approved['metadata']['source']
                    token = secrets.token_hex(32)
                    user_data, meta_data = guest.seed_data(token, approved['metadata'],
                        install_timeout=config.install_timeout, full_timeout=config.full_timeout)
                    overlay_fd, iso_fd = overlay.fileno(), iso_writer.fileno()
                    overlay_path = f'/dev/fd/{overlay_fd}'
                    disk_fds = (base_fd, overlay_fd)
                    runner.run('overlay-create', [qemu_img, 'create', '-f', 'qcow2', '-F', 'qcow2',
                        '-b', base_path, overlay_path], pass_fds=disk_fds)
                    runner.run('overlay-resize', [qemu_img, 'resize', '-f', 'qcow2', overlay_path, '40G'],
                        pass_fds=disk_fds)
                    # The boundary owns these exact bytes. Rename the fixed ISO
                    # leaf only; never decode/re-serialize or copy metadata.
                    source_fd, metadata_fd = approved['pass_fds']
                    if approved['grafts'] != [f'source.tar=/dev/fd/{source_fd}',
                                               f'metadata.json=/dev/fd/{metadata_fd}']:
                        raise vm.ProbeError('unexpected-network-payloads')
                    network_grafts = [approved['grafts'][0], f'acceptance.json=/dev/fd/{metadata_fd}']
                    payloads = (('user-data', user_data.encode()), ('meta-data', meta_data.encode()),
                                ('network-config', NETWORK_CONFIG), ('nix.tar.xz', archive))
                    with vm.payload_grafts(dirfd, payloads, deadline) as (grafts, descriptors):
                        runner.run('seed-create', [mkisofs, '-quiet', '-output', f'/dev/fd/{iso_fd}',
                            '-volid', 'cidata', *acceptance.ISO_FLAGS, *grafts, *network_grafts],
                            pass_fds=(iso_fd, *descriptors, *approved['pass_fds']))
                    raw = runner.run_qemu(network_argv(qemu, base_fd, overlay_fd, iso_readonly),
                                          (base_fd, overlay_fd, iso_readonly), token)
                    receipt = acceptance.receipt_from(raw, token, origin, transport='guest-fetch')
                    acceptance._deadline(deadline)
                # Post-yield input binding checks must succeed before acceptance.
            acceptance._deadline(deadline)
        report.update(status='passed', ok=True, fullReady=True,
                      evidence=acceptance.NETWORK_EVIDENCE, receipt=receipt)
    except KeyboardInterrupt:
        report.update(status='failure', error='cancelled')
    except vm.ProbeError as exc:
        report['error'] = str(exc)
    except (OSError, ValueError, KeyError, TypeError, guest.GuestFailure, subprocess.SubprocessError):
        report['error'] = 'host-io-or-process-error'
    if not report['ok'] and runner is not None and token is not None and origin is not None:
        report['partial'] = partial_facts(bytes(getattr(runner, 'raw', b'')), token, origin)
    return report


def main(argv=None):
    parser = vm.JsonParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True, parser_class=vm.JsonParser)
    cli = sub.add_parser('full-probe')
    for name in ('workdir', 'snapshot', 'source-store-path', 'base', 'sha256', 'qemu', 'qemu-img', 'mkisofs', 'nix-tar'):
        cli.add_argument('--' + name, required=True)
    for name in ('snapshot-device', 'snapshot-inode'):
        cli.add_argument('--' + name, type=int, required=True)
    cli.add_darwin_ps()
    cli.add_argument('--timeout', type=float, default=vm.MAX_TIMEOUT)
    cli.add_argument('--install-timeout', type=float, default=install.MAX_INSTALL_TIMEOUT)
    cli.add_argument('--full-timeout', type=float, default=7200)
    cli.add_argument('--output-limit', type=int, default=vm.MAX_OUTPUT)
    previous = {}
    try:
        def cancel(signum, frame):
            raise KeyboardInterrupt
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.signal(signum, cancel)
        args = vars(parser.parse_args(argv))
        args.pop('command')
        args['snapshot_identity'] = (args.pop('snapshot_device'), args.pop('snapshot_inode'))
        result = full_probe(NetworkConfig(**args))
    except (vm.ProbeError, KeyboardInterrupt) as exc:
        result = empty_report()
        result['error'] = 'cancelled' if isinstance(exc, KeyboardInterrupt) else str(exc)
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    print(json.dumps(result, sort_keys=True))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
