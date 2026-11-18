"""Acceptance boundary tests using synthetic files only; no Nix, VM or network."""
import base64
import copy
from dataclasses import replace
from contextlib import contextmanager
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import acceptance

snapshot = acceptance.snapshot
vm = acceptance.vm
install = acceptance.install


def store(name):
    return "/nix/store/" + "a" * 32 + "-" + name


def sri(data):
    return "sha256-" + base64.b64encode(hashlib.sha256(data).digest()).decode()


class PayloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="envy-acceptance-unit-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir(mode=0o700)
        self.source = self.root / "snapshot"
        self.source.mkdir()
        files = {"flake.lock": b'{"nodes":{"root":{}}}\n', "install.sh": b"#!/bin/sh\nexit 0\n"}
        entries = []
        for name, data in files.items():
            mode = 0o755 if name.endswith(".sh") else 0o644
            (self.source / name).write_bytes(data)
            (self.source / name).chmod(mode)
            entries.append(snapshot._entry(name, data, mode, "working-tree"))
        self.manifest = {"schemaVersion": 1, "files": sorted(entries, key=lambda row: row["relativePath"]),
                         "lockSha256": hashlib.sha256(files["flake.lock"]).hexdigest(),
                         "fixture": {"selectionStage": "minimal", "secrets": "guest-generated-only"}}
        (self.source / snapshot.MANIFEST_NAME).write_bytes(snapshot._manifest_bytes(self.manifest))
        (self.source / snapshot.MANIFEST_NAME).chmod(0o600)
        with snapshot._SnapshotIO(self.source) as source:
            self.nar_hash, self.nar_size = acceptance._source_archive(source, self.manifest, io.BytesIO(), time.monotonic() + 5)
        self.artifact = self.root / "dependencies.export"
        self.payload = b"synthetic export stream, deliberately not a valid native export"
        self.artifact.write_bytes(self.payload)
        self.artifact.chmod(0o600)
        artifact_info = self.artifact.stat()
        artifact_binding = dict(zip(('device', 'inode', 'size', 'modifiedNs', 'changedNs'), acceptance._physical(artifact_info)))
        artifact_binding['mode'] = stat.S_IMODE(artifact_info.st_mode)
        self.receipt = self.root / "preparation.json"
        rows = [{"path": store("source"), "narHash": self.nar_hash, "narSize": self.nar_size, "references": [],
                 "status": "nativeavailable", "kind": "source", "provenance": "verified-snapshot"},
                {"path": store("public"), "narHash": sri(b"public"), "narSize": 128, "references": [store("public")],
                 "status": "cached", "fileSize": 32}]
        rows.sort(key=lambda row: row["path"])
        self.plan = {"schemaVersion": 1, "status": "ready", "ready": True, "gaps": [],
                     "source": {"path": store("source"), "narHash": self.nar_hash, "lockSha256": self.manifest["lockSha256"]},
                     "lockedInputs": {"status": "complete", "count": 0},
                     "publicCache": acceptance.cache.PUBLIC_CACHE, "objects": rows,
                     "preparation": {"format": "nix-export", "complete": True, "failures": [],
                         "signatureScope": acceptance.SIGNATURE_SCOPE,
                         "objects": [{key: row[key] for key in ("path", "narHash", "narSize", "references")} for row in rows],
                         "completed": [row["path"] for row in rows], "exportedPaths": [row["path"] for row in rows],
                         "artifact": {"relativePath": self.artifact.name, "size": len(self.payload),
                                      "sha256": hashlib.sha256(self.payload).hexdigest(), "verified": True, "complete": True,
                                      "binding": artifact_binding}}}
        self.config = acceptance.FullConfig(workdir=str(self.work), snapshot=str(self.source),
                      preparation_receipt=str(self.receipt), export_artifact=str(self.artifact),
                      artifact_parent_identity=(self.root.stat().st_dev, self.root.stat().st_ino),
                      snapshot_identity=(self.source.stat().st_dev, self.source.stat().st_ino))
        self.save_plan()

    def save_plan(self):
        data = json.dumps(self.plan).encode()
        self.receipt.write_bytes(data)
        self.config = replace(self.config, preparation_size=len(data), preparation_sha256=hashlib.sha256(data).hexdigest(),
                              preparation_identity=acceptance._physical(self.receipt.stat()))

    def payloads(self, config=None):
        return acceptance.approved_payloads(config or self.config, self.work, time.monotonic() + 5)

    def reject(self, config=None):
        with self.assertRaises((vm.ProbeError, acceptance.cache.CacheError, snapshot.SnapshotError, OSError, KeyError, TypeError)):
            with self.payloads(config):
                self.fail("invalid input reached transport consumer")
        self.assertEqual(list(self.work.iterdir()), [])

    def test_bound_anonymous_readonly_transport_without_any_native_process(self):
        held = []
        with patch.object(subprocess, "Popen", side_effect=AssertionError("native process forbidden")):
            with self.payloads() as payloads:
                self.assertEqual(set(payloads), {"grafts", "pass_fds", "metadata"})
                data = {}
                for graft, fd in zip(payloads["grafts"], payloads["pass_fds"]):
                    leaf, reference = graft.split("=", 1)
                    self.assertEqual(reference, "/dev/fd/" + str(fd))
                    self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
                    self.assertEqual(os.fstat(fd).st_nlink, 0)
                    self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
                    data[leaf] = os.read(fd, 1024 * 1024)
                    held.append(fd)
                    with self.assertRaises(OSError):
                        os.write(fd, b"forbidden")
                self.assertEqual(data["dependencies.export"], self.payload)
                metadata = json.loads(data["acceptance.json"])
                self.assertEqual(metadata, payloads["metadata"])
                self.assertEqual(metadata["source"]["narHash"], self.nar_hash)
                self.assertEqual(metadata["snapshot"]["size"], len(data["source.tar"]))
                self.assertEqual(metadata["snapshot"]["sha256"], hashlib.sha256(data["source.tar"]).hexdigest())
                with tarfile.open(fileobj=io.BytesIO(data["source.tar"])) as archive:
                    self.assertEqual({row.name for row in archive}, {"source/flake.lock", "source/install.sh", "source/" + snapshot.MANIFEST_NAME})
                    self.assertTrue(all(row.isfile() for row in archive))
                self.assertEqual(list(self.work.iterdir()), [])
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(self.artifact.read_bytes(), self.payload)
        self.assertEqual(self.receipt.read_bytes(), json.dumps(self.plan).encode())
        self.assertEqual(snapshot.verify_snapshot(self.source), self.manifest)

    def test_preparation_must_be_complete_not_mock_unknown_partial_or_old_source(self):
        original = copy.deepcopy(self.plan)
        mutations = [(('ready',), False), (('ready',), 1), (('status',), 'planned'), (('gaps',), [{'kind': 'graph-failure'}]),
                     (('schemaVersion',), True), (('source', 'narHash'), sri(b'old snapshot')),
                     (('source', 'lockSha256'), '0' * 64), (('publicCache',), 'https://private.invalid'),
                     (('lockedInputs', 'status'), 'unavailable'), (('lockedInputs', 'count'), True),
                     (('preparation', 'format'), 'mock'), (('preparation', 'complete'), False),
                     (('preparation', 'signatureScope'), 'trust-host'), (('preparation', 'failures'), [{'kind': 'failed'}]),
                     (('preparation', 'objects'), []), (('preparation', 'exportedPaths'), []),
                     (('preparation', 'artifact', 'verified'), 1), (('preparation', 'artifact', 'complete'), False),
                     (('preparation', 'artifact', 'relativePath'), '../foreign'),
                     (('preparation', 'artifact', 'size'), True), (('preparation', 'artifact', 'sha256'), '0' * 64)]
        for keys, value in mutations:
            with self.subTest(keys=keys, value=value):
                self.plan = copy.deepcopy(original)
                parent = self.plan
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                self.save_plan()
                self.reject()

    def test_unknown_or_private_source_and_missing_references_are_rejected(self):
        for status, provenance, refs in (("unknown", "verified-snapshot", []),
                                         ("nativeavailable", "host-exists", []),
                                         ("cached", "verified-snapshot", [store("missing")])):
            with self.subTest(status=status):
                self.plan['objects'][0].update(status=status, provenance=provenance, references=refs)
                self.save_plan()
                self.reject()

    def test_current_manifest_cannot_reuse_old_preparation_even_with_same_lock(self):
        name = "install.sh"
        data = b"#!/bin/sh\nexit 1\n"
        (self.source / name).write_bytes(data)
        for entry in self.manifest['files']:
            if entry['relativePath'] == name:
                entry['sha256'] = hashlib.sha256(data).hexdigest()
        (self.source / snapshot.MANIFEST_NAME).write_bytes(snapshot._manifest_bytes(self.manifest))
        self.assertEqual(snapshot.verify_snapshot(self.source), self.manifest)
        self.reject()

    def test_foreign_equal_bytes_rejected_by_physical_identity_before_any_read(self):
        for target in (self.artifact, self.receipt):
            with self.subTest(target=target):
                old = target.with_suffix('.retained')
                target.rename(old)
                target.write_bytes(old.read_bytes())
                original_read = os.read
                forbidden_identity = acceptance._physical(target.stat())
                reads = []

                def read(fd, size):
                    self.assertNotEqual(acceptance._physical(os.fstat(fd)), forbidden_identity,
                                        'foreign inode was read before physical rejection')
                    reads.append(fd)
                    return original_read(fd, size)

                with patch.object(os, 'read', side_effect=read):
                    self.reject()
                old.replace(target)
                if target == self.receipt:
                    self.config = replace(self.config, preparation_identity=acceptance._physical(target.stat()))

    def test_foreign_snapshot_root_rejected_before_manifest_or_inventory_read(self):
        self.source.rename(self.root / 'retained-source')
        self.source.mkdir()
        (self.source / snapshot.MANIFEST_NAME).write_bytes(b'foreign bytes')
        with patch.object(snapshot, '_verify_snapshot', side_effect=AssertionError('foreign root read')):
            self.reject()

    def test_missing_physical_authorizations_are_fail_closed(self):
        for name in ('preparation_identity', 'snapshot_identity', 'artifact_parent_identity'):
            with self.subTest(name=name), patch.object(os, 'read', side_effect=AssertionError('unauthorized read')):
                self.reject(replace(self.config, **{name: None}))

    def test_export_binding_fields_exact_integers_mode_and_missing_binding_fail_closed(self):
        binding = copy.deepcopy(self.plan['preparation']['artifact']['binding'])
        self.assertGreater(binding['modifiedNs'], 2**53)
        for key in binding:
            for value in (binding[key] + 1, True, float(binding[key])):
                with self.subTest(key=key, value=value):
                    self.plan['preparation']['artifact']['binding'] = {**binding, key: value}
                    self.save_plan()
                    self.reject()
        self.plan['preparation']['artifact'].pop('binding')
        self.save_plan()
        self.reject()

    def test_export_hash_size_and_receipt_review_digest_cannot_be_bypassed(self):
        for data in (self.payload[:-1], b'x' * len(self.payload)):
            self.artifact.write_bytes(data)
            self.reject()
        self.artifact.write_bytes(self.payload)
        self.reject(replace(self.config, preparation_sha256='0' * 64))
        self.reject(replace(self.config, preparation_size=self.config.preparation_size + 1))
        self.reject(replace(self.config, byte_budget=16, object_byte_budget=8))

    def test_symlink_fifo_hardlink_and_foreign_parent_rejected(self):
        foreign = self.root / 'foreign'
        foreign.write_bytes(self.payload)
        self.artifact.unlink()
        self.artifact.symlink_to(foreign)
        self.reject()
        self.artifact.unlink()
        os.mkfifo(self.artifact)
        self.reject()
        self.artifact.unlink()
        os.link(foreign, self.artifact)
        self.reject()
        self.artifact.unlink()
        self.artifact.write_bytes(self.payload)
        self.reject(replace(self.config, artifact_parent_identity=(0, 0)))
        link = self.root / 'link'
        link.symlink_to(self.root, target_is_directory=True)
        self.reject(replace(self.config, export_artifact=str(link / self.artifact.name)))
        self.assertEqual(foreign.read_bytes(), self.payload)

    def test_changed_bindings_after_yield_fail_and_close_every_descriptor(self):
        for target in (self.artifact, self.receipt, self.source / 'install.sh'):
            held = []
            original = target.read_bytes()
            with self.subTest(target=target), self.assertRaises((vm.ProbeError, snapshot.SnapshotError)):
                with self.payloads() as payloads:
                    held = list(payloads['pass_fds'])
                    target.write_bytes(original)
            for fd in held:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertFalse(list(self.work.iterdir()))

    def test_replaced_artifact_cannot_redirect_bound_copy(self):
        with self.assertRaises(vm.ProbeError):
            with self.payloads() as payloads:
                self.artifact.rename(self.root / 'retained-export')
                self.artifact.write_bytes(b'foreign')
                fd = payloads['pass_fds'][1]
                self.assertEqual(os.read(fd, 10000), self.payload)
        self.assertEqual(self.artifact.read_bytes(), b'foreign')

    def test_cancellation_and_timeout_release_payloads_without_cleanup_of_inputs(self):
        held = []
        with self.assertRaises(KeyboardInterrupt):
            with self.payloads() as payloads:
                held = list(payloads['pass_fds'])
                raise KeyboardInterrupt
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)
        with self.assertRaises(vm.ProbeError):
            with acceptance.approved_payloads(self.config, self.work, time.monotonic() - 1):
                self.fail('expired deadline')
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertEqual(self.artifact.read_bytes(), self.payload)

    def test_local_guest_outputs_not_exported_and_other_arch_builds_blocked(self):
        output = {'path': store('guest-output'), 'status': 'needsguestbuild', 'kind': 'local',
                  'system': 'x86_64-linux', 'drv': store('guest.drv')}
        self.plan['objects'].append(output)
        self.save_plan()
        with self.payloads() as payloads:
            self.assertNotIn(output['path'], [r['path'] for r in payloads['metadata']['preparation']['objects']])
        output['system'] = 'aarch64-darwin'
        self.save_plan()
        self.reject()

    def test_full_probe_is_explicit_freeze_even_for_valid_input_and_never_spawns(self):
        with patch.object(subprocess, 'Popen', side_effect=AssertionError('VM forbidden')):
            result = acceptance.full_probe(self.config)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['error'], 'guest-lifecycle-not-implemented')
        for key in ('ok', 'fullReady'):
            self.assertIs(result[key], False)
        for key in ('evidence', 'receipt'):
            self.assertIsNone(result[key])
        self.assertEqual(result['commands'], [])
        for changes in ({'full_timeout': float('nan')}, {'full_timeout': 7201}, {'timeout': 601},
                        {'preparation_size': True}, {'byte_budget': True}, {'snapshot': '/tmp/unsafe,option'},
                        {'artifact_parent_identity': (True, 2)}):
            with self.subTest(changes=changes):
                result = acceptance.full_probe(replace(self.config, **changes))
                self.assertFalse(result['ok'])
                self.assertNotEqual(result['error'], 'guest-lifecycle-not-implemented')

    def test_transport_iso_flags_allow_large_export_and_no_directory_graft(self):
        self.assertEqual(acceptance.ISO_FLAGS[:2], ('-iso-level', '3'))
        with self.payloads() as payloads:
            self.assertEqual(len(payloads['grafts']), 3)
            self.assertTrue(all('=/dev/fd/' in graft for graft in payloads['grafts']))
            self.assertFalse(any(str(self.source) in graft for graft in payloads['grafts']))


class ReceiptTests(unittest.TestCase):
    def setUp(self):
        self.token = 'b' * 64
        self.source = {'path': store('source'), 'narHash': sri(b'snapshot'),
                       'lockSha256': '1' * 64, 'manifestSha256': '2' * 64}
        self.installed = {'token': self.token, 'arch': 'x86_64', 'version': install.NIX_VERSION,
                          'initialNoNix': True, 'daemon': True, 'daemonService': True, 'buildUsers': 32,
                          'profile': True, 'installerExitZero': True, 'error': None,
                          'bootstrapTools': {tool: tool != 'git' for tool in install.TOOLS},
                          'stages': {stage: True for stage in install.STAGES}}
        secrets = {'ageKeyPresent': True, 'ageKeyMode0600': True,
                   'decrypted': {name: {'exists': True, 'mode0600': True} for name in acceptance.cache.SECRETS}}
        self.row = {'schemaVersion': 1, 'evidence': acceptance.EVIDENCE, 'token': self.token, 'arch': 'x86_64',
                    'source': self.source, 'install': self.installed, 'stages': {}, 'error': None}
        self.row['stages']['setup'] = {'exitCode': 0, 'interactive': True, 'saved': True,
                                     'commitDeclined': True, 'applyDeclined': True, 'existingNixBranch': True}
        for number, name in enumerate(('firstApply', 'repeatApply', 'faultApply', 'recoveryApply'), 1):
            failed = name == 'faultApply'
            target = store('fault-generation' if failed else 'generation')
            journal = {'operationId': f'00000000-0000-0000-0000-{number:012d}', 'operation': 'apply',
                       'result': 'partial' if failed else 'ok', 'terminal': 'partial' if failed else 'ok',
                       'journalPersisted': True, 'sourceVerified': True, 'sourceFrozenPath': store('frozen'),
                       'sourceOutputPath': target, 'bindingTarget': None if failed else target,
                       'activateStatus': 'partial' if failed else 'ok', 'verifyStatus': None if failed else 'ok',
                       'conflictPartial': failed}
            stage = {'exitCode': 73 if failed else 0, 'target': target, 'journal': journal}
            if failed:
                stage.update(injectedAfter='linkGeneration', retryAllowed=False, originalMachineRestored=True,
                             partialReadableAfterRecovery=True, observedGeneration=None)
            else:
                stage.update(generation=1, managedSha256='3' * 64, permissionsSha256='4' * 64,
                             secrets=copy.deepcopy(secrets), tuiBuildTests=True)
            self.row['stages'][name] = stage

    def raw(self, row=None, installed=None):
        return (install.RECEIPT_PREFIX + json.dumps(self.installed if installed is None else installed) + '\n'
                + acceptance.RECEIPT_PREFIX + json.dumps(self.row if row is None else row) + '\n').encode()

    def test_complete_synthetic_receipt_parser_reuses_install_validator(self):
        with patch.object(install, 'receipt_from', wraps=install.receipt_from) as validator:
            result = acceptance.receipt_from(self.raw(), self.token, self.source)
        self.assertEqual(result, self.row)
        validator.assert_called_once_with(self.raw(), self.token)
        self.assertFalse(acceptance.full_probe(acceptance.FullConfig(workdir='/tmp/unused'))['ok'])

    def test_install_only_missing_or_duplicate_receipts_never_pass(self):
        cases = [b'', b'QEMU exited 0\n', self.raw().splitlines()[0], self.raw().splitlines()[1],
                 self.raw() + self.raw(), self.raw() + (acceptance.RECEIPT_PREFIX + '{}\n').encode(),
                 self.raw() + (install.RECEIPT_PREFIX + '{}\n').encode()]
        for raw in cases:
            with self.subTest(raw=raw[:50]), self.assertRaises(vm.ProbeError):
                acceptance.receipt_from(raw, self.token, self.source)

    def test_missing_stage_wrong_nonce_setup_failure_and_false_facts_are_rejected(self):
        mutations = [(('token',), 'wrong'), (('schemaVersion',), True), (('arch',), 'aarch64'),
                     (('evidence',), 'linux-vm-nix-install-only'), (('error',), 'timeout'),
                     (('stages', 'setup', 'saved'), False), (('stages', 'setup', 'exitCode'), 1),
                     (('stages', 'setup', 'interactive'), 1), (('stages', 'setup', 'commitDeclined'), False),
                     (('stages', 'firstApply', 'exitCode'), False), (('stages', 'firstApply', 'tuiBuildTests'), False),
                     (('stages', 'firstApply', 'generation'), True),
                     (('stages', 'firstApply', 'journal', 'journalPersisted'), False),
                     (('stages', 'firstApply', 'journal', 'sourceVerified'), False),
                     (('stages', 'firstApply', 'journal', 'sourceOutputPath'), store('wrong')),
                     (('stages', 'firstApply', 'journal', 'bindingTarget'), store('wrong')),
                     (('stages', 'repeatApply', 'managedSha256'), '5' * 64),
                     (('stages', 'recoveryApply', 'permissionsSha256'), '5' * 64),
                     (('stages', 'faultApply', 'exitCode'), 0), (('stages', 'faultApply', 'retryAllowed'), True),
                     (('stages', 'faultApply', 'injectedAfter'), 'before-build'),
                     (('stages', 'faultApply', 'partialReadableAfterRecovery'), False),
                     (('stages', 'faultApply', 'originalMachineRestored'), False),
                     (('stages', 'faultApply', 'journal', 'terminal'), 'ok'),
                     (('stages', 'faultApply', 'journal', 'conflictPartial'), False),
                     (('stages', 'faultApply', 'journal', 'verifyStatus'), 'ok'),
                     (('stages', 'firstApply', 'secrets', 'ageKeyMode0600'), False),
                     (('stages', 'firstApply', 'secrets', 'decrypted', 'home-passwd', 'mode0600'), False)]
        for keys, value in mutations:
            with self.subTest(keys=keys, value=value):
                row = copy.deepcopy(self.row)
                parent = row
                for key in keys[:-1]:
                    parent = parent[key]
                parent[keys[-1]] = value
                with self.assertRaises(vm.ProbeError):
                    acceptance.receipt_from(self.raw(row), self.token, self.source)
        for stage in acceptance.STAGES:
            row = copy.deepcopy(self.row)
            del row['stages'][stage]
            with self.assertRaises(vm.ProbeError):
                acceptance.receipt_from(self.raw(row), self.token, self.source)

    def test_fault_observed_generation_can_be_old_new_or_unknown_without_rollback_claim(self):
        for observed in (None, {'number': 1, 'target': store('generation')},
                         {'number': 2, 'target': store('fault-generation')}):
            self.row['stages']['faultApply']['observedGeneration'] = observed
            self.assertEqual(acceptance.receipt_from(self.raw(), self.token, self.source), self.row)

    def test_raw_secrets_commands_host_flags_and_unknown_fields_rejected_at_all_levels(self):
        positions = [(), ('install',), ('source',), ('stages',), ('stages', 'setup'),
                     ('stages', 'firstApply'), ('stages', 'firstApply', 'journal'),
                     ('stages', 'firstApply', 'secrets'),
                     ('stages', 'firstApply', 'secrets', 'decrypted', 'home-passwd'), ('stages', 'faultApply')]
        for position in positions:
            for field in ('rawSecret', 'agePrivateKey', 'guestCommand', 'hostMount', 'network'):
                row = copy.deepcopy(self.row)
                parent = row
                for key in position:
                    parent = parent[key]
                parent[field] = 'SYNTHETIC-RAW-SECRET-MUST-NOT-APPEAR'
                with self.subTest(position=position, field=field), self.assertRaises(vm.ProbeError) as caught:
                    acceptance.receipt_from(self.raw(row), self.token, self.source)
                self.assertNotIn('SYNTHETIC-RAW-SECRET', str(caught.exception))

    def test_nested_install_int_boolean_and_mismatched_install_receipt_rejected(self):
        row = copy.deepcopy(self.row)
        row['install']['installerExitZero'] = 1
        with self.assertRaises(vm.ProbeError):
            acceptance.receipt_from(self.raw(row), self.token, self.source)
        self.installed['daemonService'] = False
        with self.assertRaises(vm.ProbeError):
            acceptance.receipt_from(self.raw(), self.token, self.source)

    def test_duplicate_keys_nonfinite_json_and_oversized_receipts_rejected(self):
        for value in (b'{"token":0,"token":1}', b'{"value":NaN}', b'{broken', b'{}' + b' ' * acceptance.MAX_RECEIPT):
            raw = self.raw().splitlines()[0] + b'\n' + acceptance.RECEIPT_PREFIX.encode() + value + b'\n'
            with self.subTest(value=value[:30]), self.assertRaises(vm.ProbeError):
                acceptance.receipt_from(raw, self.token, self.source)

    def test_reused_journal_id_and_source_mismatch_rejected(self):
        self.row['stages']['repeatApply']['journal']['operationId'] = self.row['stages']['firstApply']['journal']['operationId']
        with self.assertRaises(vm.ProbeError):
            acceptance.receipt_from(self.raw(), self.token, self.source)
        source = {**self.source, 'manifestSha256': '0' * 64}
        with self.assertRaises(vm.ProbeError):
            acceptance.receipt_from(self.raw(), self.token, source)


class NetworkPayloadTests(unittest.TestCase):
    setUp = PayloadTests.setUp
    save_plan = PayloadTests.save_plan

    def network_config(self, **changes):
        return replace(acceptance.NetworkConfig(workdir=str(self.work), snapshot=str(self.source),
                       snapshot_identity=self.config.snapshot_identity, source_store_path=store('source')), **changes)

    def payloads(self, config=None, deadline=None):
        return acceptance.approved_network_payloads(config or self.network_config(), self.work,
                   time.monotonic() + 5 if deadline is None else deadline)

    def reject(self, config=None):
        with self.assertRaises((vm.ProbeError, snapshot.SnapshotError, OSError)):
            with self.payloads(config):
                self.fail('invalid network input reached consumer')
        self.assertEqual(list(self.work.iterdir()), [])

    def test_closed_metadata_actual_hashes_and_two_anonymous_readonly_fds(self):
        held = []
        original_open = os.open

        def no_store_open(path, *args, **kwargs):
            self.assertFalse(str(path).startswith('/nix/store/'), 'caller store path opened')
            return original_open(path, *args, **kwargs)

        with patch.object(subprocess, 'Popen', side_effect=AssertionError('native forbidden')), \
             patch.object(os, 'open', side_effect=no_store_open), \
             patch.object(acceptance, '_validate_plan', side_effect=AssertionError('offline plan forbidden')):
            with self.payloads() as payloads:
                self.assertEqual(set(payloads), {'grafts', 'pass_fds', 'metadata'})
                self.assertEqual(len(payloads['pass_fds']), 2)
                data = {}
                for graft, fd in zip(payloads['grafts'], payloads['pass_fds']):
                    name, ref = graft.split('=', 1)
                    self.assertEqual(ref, '/dev/fd/' + str(fd))
                    self.assertEqual(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
                    self.assertTrue(stat.S_ISREG(os.fstat(fd).st_mode))
                    self.assertEqual(os.fstat(fd).st_nlink, 0)
                    with self.assertRaises(OSError):
                        os.write(fd, b'forbidden')
                    data[name] = os.read(fd, 1024 * 1024)
                    held.append(fd)
                self.assertEqual(set(data), {'source.tar', 'metadata.json'})
                metadata = payloads['metadata']
                self.assertEqual(data['metadata.json'], (json.dumps(metadata, sort_keys=True) + '\n').encode())
                self.assertEqual(set(metadata), {'schemaVersion', 'transport', 'source', 'snapshot', 'sourceNarSize'})
                self.assertEqual(metadata['schemaVersion'], 1)
                self.assertEqual(metadata['transport'], 'guest-fetch')
                self.assertEqual(metadata['source'], {'path': store('source'), 'narHash': self.nar_hash,
                                 'lockSha256': self.manifest['lockSha256'],
                                 'manifestSha256': hashlib.sha256((self.source / snapshot.MANIFEST_NAME).read_bytes()).hexdigest()})
                self.assertIs(type(metadata['sourceNarSize']), int)
                self.assertEqual(metadata['sourceNarSize'], self.nar_size)
                self.assertEqual(metadata['snapshot'], {'size': len(data['source.tar']),
                                 'sha256': hashlib.sha256(data['source.tar']).hexdigest()})
                with tarfile.open(fileobj=io.BytesIO(data['source.tar'])) as archive:
                    self.assertTrue(all(member.isfile() for member in archive))
                    self.assertEqual(set(archive.getnames()), {'source/flake.lock', 'source/install.sh',
                                                               'source/' + snapshot.MANIFEST_NAME})
                self.assertEqual(list(self.work.iterdir()), [])
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_wrong_root_identity_causes_zero_source_body_reads(self):
        for identity in ((0, 0), (self.config.snapshot_identity[0], self.config.snapshot_identity[1] + 1)):
            with self.subTest(identity=identity), \
                 patch.object(snapshot._SnapshotIO, 'read', side_effect=AssertionError('source body read')), \
                 patch.object(snapshot, '_verify_snapshot', side_effect=AssertionError('inventory read')), \
                 patch.object(os, 'read', side_effect=AssertionError('body read')):
                self.reject(self.network_config(snapshot_identity=identity))

    def test_bad_config_and_mixed_preparation_reject_before_io_or_native(self):
        mutations = [{'snapshot': None}, {'snapshot': '/tmp/bad,option'}, {'snapshot_identity': None},
                     {'snapshot_identity': (True, 1)}, {'snapshot_identity': [1, 2]},
                     {'timeout': True}, {'timeout': 601}, {'full_timeout': float('nan')},
                     {'full_timeout': 7201}, {'install_timeout': 1201}, {'output_limit': True},
                     {'byte_budget': True}, {'object_byte_budget': 0}]
        mutations += [{'source_store_path': path} for path in
                      (None, True, store('source').removeprefix('/nix/store/'), store('wrong'),
                       store('source') + '/child', store('source') + '/', '/nix/store/' + 'e' * 32 + '-source',
                       store('source') + '/../source')]
        mutations += [{key: value} for key, value in (
            ('preparation_receipt', str(self.receipt)), ('export_artifact', str(self.artifact)),
            ('preparation_sha256', '0' * 64), ('preparation_size', 1),
            ('artifact_parent_identity', (0, 0)), ('preparation_identity', (0, 0, 0, 0, 0)))]
        for changes in mutations:
            with self.subTest(changes=changes), patch.object(os, 'open', side_effect=AssertionError('config IO')), \
                 patch.object(subprocess, 'Popen', side_effect=AssertionError('native forbidden')):
                self.reject(self.network_config(**changes))
        self.reject(self.config)

    def test_fixture_manifest_inventory_and_content_drift_rejected(self):
        manifest_path = self.source / snapshot.MANIFEST_NAME
        original = manifest_path.read_bytes()
        for key, value in (('selectionStage', 'full'), ('secrets', 'host')):
            manifest = copy.deepcopy(self.manifest)
            manifest['fixture'][key] = value
            manifest_path.write_bytes(snapshot._manifest_bytes(manifest))
            self.reject()
        manifest_path.write_bytes(original)
        (self.source / 'unexpected').write_bytes(b'unapproved')
        self.reject()
        (self.source / 'unexpected').unlink()
        (self.source / 'install.sh').write_bytes(b'changed')
        self.reject()

    def test_source_and_root_changes_after_yield_close_fds_and_fail(self):
        for change_root in (False, True):
            held = []
            with self.subTest(change_root=change_root), self.assertRaises(snapshot.SnapshotError):
                with self.payloads() as payloads:
                    held = payloads['pass_fds']
                    if change_root:
                        self.source.rename(self.root / 'retained-source')
                        self.source.mkdir()
                    else:
                        source = self.source / 'install.sh'
                        source.write_bytes(source.read_bytes())
            for fd in held:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertEqual(list(self.work.iterdir()), [])

    def test_budget_nar_cap_and_archive_read_size_cannot_be_bypassed(self):
        with patch.object(acceptance, 'MAX_SOURCE', 1):
            self.reject()
        original = acceptance._source_archive
        for size in (True, 0, acceptance.MAX_SOURCE * 2 + 1):
            def oversized(*args):
                nar, _ = original(*args)
                return nar, size
            with self.subTest(size=size), patch.object(acceptance, '_source_archive', side_effect=oversized):
                self.reject()
        def oversized_tar(source, manifest, writer, deadline):
            result = original(source, manifest, writer, deadline)
            writer.seek(16384)
            writer.write(b'x')
            return result

        with patch.object(acceptance, 'MAX_SOURCE', 8192), \
             patch.object(acceptance, '_source_archive', side_effect=oversized_tar):
            self.reject()

    def test_deadline_before_read_after_yield_and_cancellation_close_fds(self):
        with patch.object(snapshot._SnapshotIO, 'read', side_effect=AssertionError('expired source read')):
            with self.assertRaisesRegex(vm.ProbeError, 'timeout'):
                with self.payloads(deadline=time.monotonic() - 1):
                    self.fail('expired deadline')
        held = []
        with self.assertRaises(KeyboardInterrupt):
            with self.payloads() as payloads:
                held = payloads['pass_fds']
                raise KeyboardInterrupt
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)
        with patch.object(acceptance.time, 'monotonic', return_value=1) as clock:
            with self.assertRaisesRegex(vm.ProbeError, 'timeout'):
                with self.payloads(deadline=10) as payloads:
                    held = payloads['pass_fds']
                    clock.return_value = 11

        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_read_failure_and_partial_transport_creation_cleanup(self):
        original = snapshot._SnapshotIO.read
        calls = 0

        def fail_read(source, name):
            nonlocal calls
            calls += 1
            if calls == 5:
                raise OSError('synthetic read failure')
            return original(source, name)

        with patch.object(snapshot._SnapshotIO, 'read', new=fail_read):
            self.reject()
        original_transport = acceptance.cache.archive_transport
        held = []

        def fail_second(work):
            if held:
                raise OSError('synthetic allocation failure')
            @contextmanager
            def first():
                with original_transport(work) as pair:
                    held.append(pair[1])
                    yield pair
            return first()

        with patch.object(acceptance.cache, 'archive_transport', side_effect=fail_second):
            self.reject()
        for fd in held:
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_tar_read_failure_short_read_and_mid_hash_timeout_release_fds(self):
        original_transport = acceptance.cache.archive_transport
        for failure in ('error', 'short', 'timeout'):
            held = []

            @contextmanager
            def transport(work):
                with original_transport(work) as (stream, fd):
                    held.append(fd)
                    original_read = stream.read

                    def read(size):
                        if failure == 'error':
                            raise OSError('synthetic archive read failure')
                        if failure == 'short':
                            return b''
                        clock.return_value = 11
                        return original_read(size)

                    with patch.object(stream, 'read', side_effect=read):
                        yield stream, fd

            with self.subTest(failure=failure), \
                 patch.object(acceptance.time, 'monotonic', return_value=1) as clock, \
                 patch.object(acceptance.cache, 'archive_transport', side_effect=transport):
                with self.assertRaises((OSError, vm.ProbeError)):
                    with self.payloads(deadline=10):
                        self.fail('bad archive read reached consumer')
            for fd in held:
                with self.assertRaises(OSError):
                    os.fstat(fd)
            self.assertEqual(list(self.work.iterdir()), [])

    def test_staging_namespace_needs_no_claim_of_directory_ownership(self):
        self.work.chmod(0o777)
        sentinel = self.work / 'foreign'
        sentinel.write_bytes(b'foreign')
        with self.payloads() as payloads:
            self.assertEqual(len(payloads['pass_fds']), 2)
            self.assertEqual(list(self.work.iterdir()), [sentinel])
        self.assertEqual(sentinel.read_bytes(), b'foreign')

    def test_network_config_delegates_to_closed_full_probe(self):
        config, sentinel = self.network_config(), object()
        with patch('full.full_probe', return_value=sentinel) as dispatch,                 patch.object(subprocess, 'Popen', side_effect=AssertionError('native forbidden')):
            self.assertIs(acceptance.full_probe(config), sentinel)
        dispatch.assert_called_once_with(config)


class NetworkReceiptTests(unittest.TestCase):
    raw = ReceiptTests.raw

    def setUp(self):
        ReceiptTests.setUp(self)
        self.row.update(transport='guest-fetch', evidence=acceptance.NETWORK_EVIDENCE)

    def consume(self, raw=None, source=None, transport='guest-fetch'):
        return acceptance.receipt_from(self.raw() if raw is None else raw, self.token,
                                       self.source if source is None else source, transport=transport)

    def test_network_receipt_reuses_real_install_and_stage_guards(self):
        with patch.object(install, 'receipt_from', wraps=install.receipt_from) as validator:
            self.assertEqual(self.consume(), self.row)
        validator.assert_called_once_with(self.raw(), self.token)

    def test_transport_evidence_and_offline_frames_cannot_cross(self):
        with self.assertRaises(vm.ProbeError):
            acceptance.receipt_from(self.raw(), self.token, self.source)
        for key, value in (('transport', 'nix-export'), ('transport', True), ('evidence', acceptance.EVIDENCE),
                           ('evidence', 'linux-vm-nix-install-only'), ('token', 'wrong'), ('arch', 'aarch64'),
                           ('schemaVersion', True), ('error', 'timeout')):
            row = {**self.row, key: value}
            with self.subTest(key=key, value=value), self.assertRaises(vm.ProbeError):
                self.consume(self.raw(row))
        offline = {key: value for key, value in self.row.items() if key != 'transport'}
        offline['evidence'] = acceptance.EVIDENCE
        with self.assertRaises(vm.ProbeError):
            self.consume(self.raw(offline))
        for transport in ('unknown', None, True, 1):
            with self.subTest(transport=transport), self.assertRaises(vm.ProbeError):
                self.consume(transport=transport)

    def test_all_existing_negative_stage_unknown_key_and_json_cases_apply_to_network(self):
        # Reuse the old hostile frames with the network transport selected.
        original = acceptance.receipt_from

        def network(raw, token, source):
            return original(raw, token, source, transport='guest-fetch')

        for method in (ReceiptTests.test_install_only_missing_or_duplicate_receipts_never_pass,
                       ReceiptTests.test_missing_stage_wrong_nonce_setup_failure_and_false_facts_are_rejected,
                       ReceiptTests.test_raw_secrets_commands_host_flags_and_unknown_fields_rejected_at_all_levels,
                       ReceiptTests.test_nested_install_int_boolean_and_mismatched_install_receipt_rejected,
                       ReceiptTests.test_duplicate_keys_nonfinite_json_and_oversized_receipts_rejected,
                       ReceiptTests.test_reused_journal_id_and_source_mismatch_rejected):
            self.setUp()
            with self.subTest(method=method.__name__), patch.object(acceptance, 'receipt_from', side_effect=network):
                method(self)

    def test_source_exact_keys_and_missing_install_guard_rejected(self):
        for source in ({**self.source, 'sourceNarSize': 100}, {**self.source, 'path': store('other')},
                       {**self.source, 'manifestSha256': '0' * 64}):
            with self.subTest(source=source), self.assertRaises(vm.ProbeError):
                self.consume(source=source)
        for key in self.installed:
            installed = {name: value for name, value in self.installed.items() if name != key}
            row = {**self.row, 'install': installed}
            with self.subTest(missing=key), self.assertRaises(vm.ProbeError):
                self.consume(self.raw(row, installed))


class NarContractTests(unittest.TestCase):
    def test_regular_file_encoding_matches_explicit_nar_wire_fixture(self):
        # A hand-specified wire fixture, independent of tar extraction or Nix.
        class Source:
            def read(self, name):
                return (b'x', 0o755) if name == 'install.sh' else (b'{}', 0o600)

            def check(self):
                pass

        manifest = {'files': [{'relativePath': 'install.sh', 'sha256': hashlib.sha256(b'x').hexdigest()}]}
        strings = [b'nix-archive-1', b'(', b'type', b'directory',
                   b'entry', b'(', b'name', b'install.sh', b'node', b'(', b'type', b'regular',
                   b'executable', b'', b'contents', b'x', b')', b')',
                   b'entry', b'(', b'name', b'snapshot-manifest.json', b'node', b'(', b'type', b'regular',
                   b'contents', b'{}', b')', b')', b')']
        wire = b''.join(struct.pack('<Q', len(item)) + item + b'\0' * (-len(item) % 8) for item in strings)
        observed, size = acceptance._source_archive(Source(), manifest, io.BytesIO(), time.monotonic() + 5)
        self.assertEqual(observed, sri(wire))
        self.assertEqual(size, len(wire))


if __name__ == '__main__':
    unittest.main()
