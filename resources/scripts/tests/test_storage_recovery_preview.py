"""Recovery previews and approvals use real temporary files and fake archives only."""

import hashlib
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from envy.storage import backend, lifecycle
from envy.storage.model import Policy, StorageError, signature, tree_digest
from envy.storage.state import read_record, write_record


class RecoveryPreviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.state = self.root / "state"
        self.policy = Policy(self.root, "manual", 1, (), (), (), (), "", "")
        self.target = self.root / "recovery"
        self.archive = patch.object(lifecycle, "Archive", autospec=True).start()
        self.addCleanup(patch.stopall)
        self.archive.return_value.identity.return_value = "a" * 64
        self.archive.return_value.content_hash.return_value = hashlib.sha256(b"fixture").hexdigest()
        self.archive.return_value.restore_file.side_effect = lambda snapshot, path, fd, digest: os.write(fd, b"fixture")
        patch.object(lifecycle, "assert_inactive").start()

    def batch(self, *, action="quarantine", held=True, expired=True, directory=False):
        identifier = uuid.uuid4().hex
        path = self.state / "quarantine" / identifier / "0"
        path.parent.mkdir(parents=True)
        if directory:
            path.mkdir()
            (path / "nested").mkdir()
            (path / "nested" / "file").write_bytes(b"fixture")
        else:
            path.write_bytes(b"fixture")
        item = {"path": str(self.root / "original" / identifier), "action": action,
                "targets": "dirs" if directory else "files", "status": "archived-held" if action == "archive" else "quarantined",
                "expiresAt": 0 if expired else 10**12, "size": 7,
                "heldPath": str(path), "signature": signature(path.lstat()),
                "heldSignature": signature(path.lstat())}
        if directory:
            item["treeHash"] = tree_digest(path)
        if action == "archive":
            item["sha256"] = hashlib.sha256(b"fixture").hexdigest()
        if not held:
            path.unlink()
            item["status"] = "archived" if action == "archive" else "expired"
        batch = {"id": identifier, "snapshot": "b" * 64 if action == "archive" else None,
                 "repositoryId": "a" * 64 if action == "archive" else None, "items": [item]}
        write_record(self.state, "runs", batch)
        return batch

    def preview(self, batch):
        return lifecycle.preview_restore(self.policy, self.state, batch["id"], self.target)

    def restore(self, batch, preview):
        return lifecycle.restore(self.policy, self.state, batch["id"], self.target,
                                 approved_preview=preview)

    def test_held_preview_does_not_create_target_or_parents_or_call_archive(self):
        batch = self.batch(action="archive")
        self.target = self.root / "missing" / "nested" / "recovery"
        before = sorted(str(path) for path in self.root.rglob("*"))
        with patch.object(Path, "mkdir", side_effect=AssertionError("preview mkdir")):
            preview = self.preview(batch)
        self.assertEqual(preview["items"][0]["status"], "held")
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob("*")))
        self.assertFalse(preview["metadata"]["archiveReadRequired"])
        self.archive.return_value.identity.assert_not_called()
        self.archive.return_value.restore.assert_not_called()
        self.assertEqual(preview["targetBinding"]["missingParents"],
                         [str(self.root / "missing"), str(self.root / "missing" / "nested")])
        json.dumps(preview)

    def test_remote_preview_only_reads_and_approved_restore_streams_to_exclusive_fd(self):
        batch = self.batch(action="archive", held=False)
        preview = self.preview(batch)
        self.assertEqual(preview["items"][0]["status"], "remote")
        self.assertTrue(preview["metadata"]["archiveReadRequired"])
        self.assertTrue(preview["metadata"]["executionSupported"])
        self.archive.return_value.restore.assert_not_called()
        self.archive.return_value.restore_file.assert_not_called()
        self.assertFalse(self.target.exists())
        restored = self.restore(batch, preview)
        self.assertEqual(restored["restored"], [batch["items"][0]["path"]])
        destination = self.target / Path(batch["items"][0]["path"]).relative_to("/")
        self.assertEqual(destination.read_bytes(), b"fixture")
        self.archive.return_value.restore.assert_not_called()
        self.archive.return_value.restore_file.assert_called_once()

    def test_missing_quarantine_is_reported_unavailable(self):
        preview = self.preview(self.batch(held=False))
        self.assertEqual(preview["items"][0]["status"], "unavailable")
        self.archive.return_value.identity.assert_not_called()
        self.assertFalse(self.target.exists())

    def test_archive_identity_and_content_false_fail_without_target_write(self):
        batch = self.batch(action="archive", held=False)
        for name in ("identity", "content_hash"):
            with self.subTest(name=name):
                method = getattr(self.archive.return_value, name)
                original = method.return_value
                method.return_value = "wrong"
                with self.assertRaises(StorageError):
                    self.preview(batch)
                method.return_value = original
                self.assertFalse(self.target.exists())
        self.archive.return_value.restore.assert_not_called()

    def test_existing_relative_symlink_and_parent_symlink_targets_rejected(self):
        batch = self.batch()
        self.target.mkdir()
        occupied = self.target
        link = self.root / "alias"
        link.symlink_to(occupied, target_is_directory=True)
        for target in (occupied, Path("relative"), link, link / "new"):
            with self.subTest(target=target), self.assertRaises((StorageError, OSError)):
                lifecycle.preview_restore(self.policy, self.state, batch["id"], target)
        self.assertEqual(list(occupied.iterdir()), [])

    def test_approved_held_restore_uses_shared_copy_and_preserves_source(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                batch = self.batch(directory=directory)
                self.target = self.root / uuid.uuid4().hex / "new" / "recovery"
                preview = self.preview(batch)
                result = self.restore(batch, preview)
                item = batch["items"][0]
                output = self.target / Path(item["path"]).relative_to("/")
                if directory:
                    self.assertEqual((output / "nested" / "file").read_bytes(), b"fixture")
                else:
                    self.assertEqual(output.read_bytes(), b"fixture")
                self.assertEqual(result["restored"], [item["path"]])
                self.assertTrue(Path(item["heldPath"]).exists())
        self.archive.return_value.restore.assert_not_called()

    def test_manifest_inode_and_content_drift_rejected_before_target_creation(self):
        for drift in ("manifest", "inode", "content"):
            with self.subTest(drift=drift):
                batch = self.batch()
                preview = self.preview(batch)
                held = Path(batch["items"][0]["heldPath"])
                if drift == "manifest":
                    batch["items"][0]["expiresAt"] = 22
                    write_record(self.state, "runs", batch)
                elif drift == "inode":
                    replacement = held.with_name("replacement")
                    replacement.write_bytes(held.read_bytes())
                    held.unlink()
                    replacement.rename(held)
                else:
                    held.write_bytes(b"changed")
                with self.assertRaises(StorageError):
                    self.restore(batch, preview)
                self.assertFalse(self.target.exists())

    def test_directory_content_drift_with_preserved_metadata_is_rejected(self):
        batch = self.batch(directory=True)
        preview = self.preview(batch)
        held = Path(batch["items"][0]["heldPath"])
        child = held / "nested" / "file"
        before = child.stat()
        child.write_bytes(b"changed")
        os.utime(child, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(tree_digest(held), batch["items"][0]["treeHash"])
        with self.assertRaisesRegex(StorageError, "evidence changed"):
            self.restore(batch, preview)
        self.assertFalse(self.target.exists())

    def test_parent_alias_replacement_rejected(self):
        batch = self.batch()
        parent = self.root / "parent"
        parent.mkdir()
        self.target = parent / "recovery"
        preview = self.preview(batch)
        parent.rename(self.root / "original-parent")
        parent.mkdir()
        with self.assertRaisesRegex(StorageError, "evidence changed"):
            self.restore(batch, preview)
        self.assertFalse(self.target.exists())

    def test_parent_replacement_between_recheck_and_mkdir_rejected(self):
        batch = self.batch()
        parent = self.root / "parent"
        parent.mkdir()
        self.target = parent / "recovery"
        preview = self.preview(batch)
        original = lifecycle._target_binding
        calls = 0

        def replace(target):
            nonlocal calls
            result = original(target)
            calls += 1
            if calls == 2:
                parent.rename(self.root / "old-parent")
                parent.mkdir()
            return result

        with patch.object(lifecycle, "_target_binding", side_effect=replace):
            with self.assertRaisesRegex(StorageError, "parent identity"):
                self.restore(batch, preview)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.root / "old-parent" / "recovery").exists())

    def test_concurrent_target_creation_never_overwrites_competitor(self):
        batch = self.batch()
        preview = self.preview(batch)
        mkdir = os.mkdir

        def occupy(path, *args, **kwargs):
            if path == self.target.name:
                self.target.mkdir()
                (self.target / "keep").write_bytes(b"competitor")
            return mkdir(path, *args, **kwargs)

        with patch.object(lifecycle.os, "mkdir", side_effect=occupy):
            with self.assertRaises(FileExistsError):
                self.restore(batch, preview)
        self.assertEqual((self.target / "keep").read_bytes(), b"competitor")
        self.assertEqual(list(self.target.iterdir()), [self.target / "keep"])

    def test_competing_output_file_is_not_overwritten(self):
        batch = self.batch()
        preview = self.preview(batch)
        copy = lifecycle._copy_entry
        occupied = []

        def occupy(source, parent, name, check):
            descriptor = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
            os.write(descriptor, b"competitor")
            os.close(descriptor)
            occupied.append(name)
            copy(source, parent, name, check)

        with patch.object(lifecycle, "_copy_entry", side_effect=occupy):
            with self.assertRaises(FileExistsError):
                self.restore(batch, preview)
        self.assertTrue(occupied)
        destination = self.target / Path(batch["items"][0]["path"]).relative_to("/")
        self.assertEqual(destination.read_bytes(), b"competitor")

    def test_purge_preview_has_evidence_and_never_deletes_or_writes_records(self):
        batch = self.batch()
        record = self.state / "runs" / (batch["id"] + ".json")
        before = record.read_bytes()
        with patch.object(lifecycle, "_release", side_effect=AssertionError("preview release")), patch.object(
                lifecycle, "write_record", side_effect=AssertionError("preview write")):
            preview = lifecycle.preview_purge(self.policy, self.state)
        self.assertEqual(preview["approvedRuns"], [batch["id"]])
        self.assertTrue(preview["eligible"][0]["items"][0]["sha256"])
        self.assertEqual(record.read_bytes(), before)
        self.assertTrue(Path(batch["items"][0]["heldPath"]).exists())

    def test_approved_purge_does_not_collect_newly_eligible_run(self):
        approved = self.batch()
        later = self.batch(expired=False)
        preview = lifecycle.preview_purge(self.policy, self.state)
        later["items"][0]["expiresAt"] = 0
        write_record(self.state, "runs", later)
        result = lifecycle.purge(self.policy, self.state, execute=True, approved_runs=preview["approvedRuns"], approved_preview=preview)
        self.assertEqual(result["errors"], [])
        self.assertFalse(Path(approved["items"][0]["heldPath"]).exists())
        self.assertTrue(Path(later["items"][0]["heldPath"]).exists())

    def test_purge_removed_or_invalidated_approved_runs_report_errors(self):
        missing = uuid.uuid4().hex
        invalid = self.batch(expired=False)
        result = lifecycle.purge(self.policy, self.state, execute=True,
                                 approved_runs=[missing, invalid["id"]])
        self.assertEqual(len(result["errors"]), 2)
        self.assertTrue(Path(invalid["items"][0]["heldPath"]).exists())

    def test_partial_purge_failure_preserves_success_record_and_failed_source(self):
        good, bad = self.batch(), self.batch()
        preview = lifecycle.preview_purge(self.policy, self.state)
        Path(bad["items"][0]["heldPath"]).write_bytes(b"changed")
        result = lifecycle.purge(self.policy, self.state, execute=True, approved_runs=preview["approvedRuns"], approved_preview=preview)
        self.assertEqual(len(result["errors"]), 1)
        self.assertEqual(read_record(self.state, "runs", good["id"])["items"][0]["status"], "expired")
        self.assertEqual(read_record(self.state, "runs", bad["id"])["items"][0]["status"], "quarantined")
        self.assertTrue(Path(bad["items"][0]["heldPath"]).exists())

    def test_archive_retention_failure_blocks_purge(self):
        batch = self.batch(action="archive")
        preview = lifecycle.preview_purge(self.policy, self.state)
        self.archive.return_value.content_hash.return_value = "false"
        result = lifecycle.purge(self.policy, self.state, execute=True, approved_runs=preview["approvedRuns"], approved_preview=preview)
        self.assertEqual(len(result["errors"]), 1)
        self.assertTrue(Path(batch["items"][0]["heldPath"]).exists())

    def test_same_run_newly_eligible_item_is_outside_approved_purge(self):
        batch = self.batch()
        other = self.batch(expired=False)
        item = other["items"][0]
        old = Path(item["heldPath"])
        held = Path(batch["items"][0]["heldPath"]).with_name("1")
        old.rename(held)
        item.update(heldPath=str(held), heldSignature=signature(held.lstat()))
        batch["items"].append(item)
        write_record(self.state, "runs", batch)
        preview = lifecycle.preview_purge(self.policy, self.state)
        item["expiresAt"] = 0
        write_record(self.state, "runs", batch)
        result = lifecycle.purge(self.policy, self.state, execute=True, approved_preview=preview)
        self.assertEqual(result["errors"], [])
        self.assertTrue(held.exists())
        self.assertFalse(Path(batch["items"][0]["heldPath"]).exists())

    def test_removed_and_expiry_changed_approved_items_are_explicit_errors(self):
        for change in ("removed", "expiry"):
            with self.subTest(change=change):
                batch = self.batch()
                preview = lifecycle.preview_purge(self.policy, self.state)
                if change == "removed":
                    batch["items"] = []
                else:
                    batch["items"][0]["expiresAt"] = 1
                write_record(self.state, "runs", batch)
                result = lifecycle.purge(self.policy, self.state, execute=True, approved_preview=preview)
                self.assertTrue(any(error["run"] == batch["id"] for error in result["errors"]))

    def test_remote_execution_rechecks_identity_and_stream_digest(self):
        for drift in ("identity", "content", "stream"):
            with self.subTest(drift=drift):
                batch = self.batch(action="archive", held=False)
                self.target = self.root / uuid.uuid4().hex
                self.archive.return_value.identity.return_value = "a" * 64
                self.archive.return_value.content_hash.return_value = hashlib.sha256(b"fixture").hexdigest()
                preview = self.preview(batch)
                if drift == "identity":
                    self.archive.return_value.identity.return_value = "c" * 64
                elif drift == "content":
                    self.archive.return_value.content_hash.return_value = "changed"
                else:
                    self.archive.return_value.restore_file.side_effect = lambda snap, path, fd, digest: os.write(fd, b"corrupt")
                with self.assertRaises(StorageError):
                    self.restore(batch, preview)
                if drift != "stream":
                    self.assertFalse(self.target.exists())
                self.archive.return_value.restore.assert_not_called()

    def test_unknown_remote_contract_is_rejected_without_writes(self):
        batch = self.batch(action="archive", held=False)
        batch["items"][0]["targets"] = "dirs"
        write_record(self.state, "runs", batch)
        with self.assertRaisesRegex(StorageError, "single-file"):
            self.preview(batch)
        self.assertFalse(self.target.exists())
        self.archive.return_value.restore_file.assert_not_called()

    def test_mixed_held_remote_and_unavailable_share_one_restore_flow(self):
        batch = self.batch()
        remote = self.batch(action="archive", held=False)
        unavailable = self.batch(held=False)
        remote["items"][0].pop("heldPath")
        unavailable["items"][0].pop("heldPath")
        batch["items"].extend([remote["items"][0], unavailable["items"][0]])
        batch.update(snapshot=remote["snapshot"], repositoryId=remote["repositoryId"])
        write_record(self.state, "runs", batch)
        preview = self.preview(batch)
        self.assertEqual([item["status"] for item in preview["items"]], ["held", "remote", "unavailable"])
        restored = self.restore(batch, preview)
        self.assertEqual(restored["restored"], [item["path"] for item in batch["items"][:2]])
        self.assertEqual(restored["unavailable"], [{"path": unavailable["items"][0]["path"], "status": "expired"}])
        self.archive.return_value.restore.assert_not_called()

    def test_remote_competing_output_is_never_overwritten_or_streamed(self):
        batch = self.batch(action="archive", held=False)
        preview = self.preview(batch)
        output = self.target / Path(batch["items"][0]["path"]).relative_to("/")
        original_open = os.open

        def occupy(path, flags, *args, **kwargs):
            if path == output.name and flags & os.O_CREAT:
                descriptor = original_open(path, flags, *args, **kwargs)
                os.write(descriptor, b"competitor")
                os.close(descriptor)
            return original_open(path, flags, *args, **kwargs)

        with patch.object(lifecycle.os, "open", side_effect=occupy):
            with self.assertRaises(FileExistsError):
                self.restore(batch, preview)
        self.assertEqual(output.read_bytes(), b"competitor")
        self.archive.return_value.restore_file.assert_not_called()

    def test_remote_output_renamed_and_replaced_during_stream_is_rejected(self):
        for replacement in (b"competitor", b"fixture"):
            with self.subTest(replacement=replacement):
                batch = self.batch(action="archive", held=False)
                self.target = self.root / uuid.uuid4().hex
                preview = self.preview(batch)
                output = self.target / Path(batch["items"][0]["path"]).relative_to("/")
                sibling = output.with_name(output.name + ".detached")

                def replace(snapshot, path, descriptor, digest):
                    original = os.fstat(descriptor)
                    output.rename(sibling)
                    output.write_bytes(replacement)
                    os.write(descriptor, b"fixture")
                    current = os.fstat(descriptor)
                    self.assertEqual((current.st_dev, current.st_ino, current.st_nlink),
                                     (original.st_dev, original.st_ino, 1))
                    self.assertEqual(lifecycle._fd_digest(descriptor), digest)
                    self.assertNotEqual(output.stat().st_ino, current.st_ino)

                self.archive.return_value.restore_file.side_effect = replace
                with self.assertRaisesRegex(StorageError, "output entry binding changed"):
                    self.restore(batch, preview)
                self.assertEqual(output.read_bytes(), replacement)
                self.assertEqual(sibling.read_bytes(), b"fixture")
                self.archive.return_value.restore.assert_not_called()

    def test_held_output_renamed_and_replaced_during_copy_is_rejected(self):
        batch = self.batch()
        preview = self.preview(batch)
        output = self.target / Path(batch["items"][0]["path"]).relative_to("/")
        sibling = output.with_name(output.name + ".detached")
        copy = lifecycle.shutil.copyfileobj

        def replace(source, destination):
            copy(source, destination)
            output.rename(sibling)
            # Equal content must not hide replacement of the exclusively created inode.
            output.write_bytes(b"fixture")

        with patch.object(lifecycle.shutil, "copyfileobj", side_effect=replace):
            with self.assertRaisesRegex(StorageError, "output entry binding changed"):
                self.restore(batch, preview)
        self.assertEqual(output.read_bytes(), b"fixture")
        self.assertEqual(sibling.read_bytes(), b"fixture")
        self.assertTrue(Path(batch["items"][0]["heldPath"]).exists())

    def test_held_output_replaced_during_verification_is_rejected(self):
        for directory in (False, True):
            with self.subTest(directory=directory):
                batch = self.batch(directory=directory)
                self.target = self.root / uuid.uuid4().hex
                preview = self.preview(batch)
                output = self.target / Path(batch["items"][0]["path"]).relative_to("/")
                sibling = output.with_name(output.name + ".detached")
                digest = lifecycle._fd_digest
                replaced = False

                def replace(descriptor):
                    nonlocal replaced
                    result = digest(descriptor)
                    if output.exists() and not replaced:
                        current = os.fstat(descriptor)
                        linked = output.lstat()
                        if (current.st_dev, current.st_ino) == (linked.st_dev, linked.st_ino):
                            output.rename(sibling)
                            output.write_bytes(b"competitor")
                            replaced = True
                    return result

                with patch.object(lifecycle, "_fd_digest", side_effect=replace):
                    with self.assertRaisesRegex(StorageError, "output entry binding changed"):
                        self.restore(batch, preview)
                self.assertTrue(replaced)
                self.assertEqual(output.read_bytes(), b"competitor")
                self.assertEqual((sibling / "nested" / "file" if directory else sibling).read_bytes(),
                                 b"fixture")
                self.assertTrue(Path(batch["items"][0]["heldPath"]).exists())

    def recovery_batch(self, *, held, count=2, unavailable=False):
        batch = self.batch(action="quarantine" if held else "archive", held=held)
        for index in range(1, count):
            other = self.batch(action="quarantine" if held else "archive", held=held)
            item = other["items"][0]
            if held:
                source = Path(item["heldPath"])
                destination = Path(batch["items"][0]["heldPath"]).with_name(str(index))
                source.rename(destination)
                item.update(heldPath=str(destination), heldSignature=signature(destination.lstat()))
            else:
                item.pop("heldPath")
            batch["items"].append(item)
        if unavailable:
            item = self.batch(held=False)["items"][0]
            item.pop("heldPath")
            batch["items"].append(item)
        write_record(self.state, "runs", batch)
        return batch

    def test_partial_recovery_preserves_success_failed_output_and_unattempted_items(self):
        for held in (True, False):
            for failure in ("write", "replace"):
                for count in (2, 3):
                    with self.subTest(held=held, failure=failure, count=count):
                        batch = self.recovery_batch(held=held, count=count, unavailable=True)
                        self.target = self.root / uuid.uuid4().hex
                        preview = self.preview(batch)
                        paths = [item["path"] for item in batch["items"]]
                        outputs = [self.target / Path(path).relative_to("/") for path in paths]
                        sibling = outputs[1].with_name(outputs[1].name + ".detached")
                        copy = lifecycle.shutil.copyfileobj
                        calls = 0

                        def fail_output(descriptor):
                            if failure == "write":
                                os.write(descriptor, b"part")
                                raise OSError("private native diagnostic must not enter result")
                            os.write(descriptor, b"fixture")
                            outputs[1].rename(sibling)
                            outputs[1].write_bytes(b"competitor")

                        def held_copy(source, destination):
                            nonlocal calls
                            calls += 1
                            if calls == 2:
                                fail_output(destination.fileno())
                            else:
                                copy(source, destination)

                        def remote_copy(snapshot, path, descriptor, digest):
                            nonlocal calls
                            calls += 1
                            if calls == 2:
                                fail_output(descriptor)
                            else:
                                os.write(descriptor, b"fixture")

                        self.archive.return_value.restore_file.side_effect = remote_copy
                        with patch.object(lifecycle.shutil, "copyfileobj", side_effect=held_copy):
                            with self.assertRaises(lifecycle.RecoveryPartialError) as caught:
                                self.restore(batch, preview)
                        result = caught.exception.result
                        self.assertIsInstance(caught.exception, StorageError)
                        self.assertEqual(result["run"], batch["id"])
                        self.assertEqual(result["target"], str(self.target))
                        self.assertEqual(result["restored"], paths[:1])
                        self.assertEqual([item["path"] for item in result["errors"]], paths[1:2])
                        self.assertEqual([item["path"] for item in result["unknown"]], paths[1:2])
                        self.assertEqual([item["path"] for item in result["notAttempted"]], paths[2:count])
                        self.assertEqual(result["unavailable"], [{"path": paths[-1], "status": "expired"}])
                        self.assertNotIn("private native diagnostic", json.dumps(result))
                        self.assertEqual(outputs[0].read_bytes(), b"fixture")
                        self.assertEqual(outputs[1].read_bytes(), b"part" if failure == "write" else b"competitor")
                        self.assertTrue(all(not output.exists() for output in outputs[2:]))
                        if failure == "replace":
                            self.assertEqual(sibling.read_bytes(), b"fixture")
                        if held:
                            self.assertTrue(all(Path(item["heldPath"]).read_bytes() == b"fixture"
                                                for item in batch["items"][:count]))
                        self.assertEqual(calls, 2)
                        self.archive.return_value.restore.assert_not_called()

    def test_final_target_check_failure_preserves_verified_progress_and_marks_paths_unknown(self):
        for held in (True, False):
            with self.subTest(held=held):
                batch = self.recovery_batch(held=held, unavailable=True)
                self.target = self.root / uuid.uuid4().hex
                preview = self.preview(batch)
                selected = lifecycle._restore_selected
                detached = self.target.with_name(self.target.name + ".detached")

                def replace(*args, **kwargs):
                    restored = selected(*args, **kwargs)
                    self.target.rename(detached)
                    self.target.mkdir()
                    (self.target / "keep").write_bytes(b"competitor")
                    return restored

                self.archive.return_value.restore_file.side_effect = lambda snap, path, fd, digest: os.write(fd, b"fixture")
                with patch.object(lifecycle, "_restore_selected", side_effect=replace):
                    with self.assertRaisesRegex(lifecycle.RecoveryPartialError, "binding changed") as caught:
                        self.restore(batch, preview)
                result = caught.exception.result
                paths = [item["path"] for item in batch["items"][:2]]
                self.assertEqual(result["run"], batch["id"])
                self.assertEqual(result["target"], str(self.target))
                self.assertEqual(result["restored"], paths)
                self.assertEqual([item["path"] for item in result["unknown"]], paths)
                self.assertTrue(all("not revalidated" in item["reason"] for item in result["unknown"]))
                self.assertTrue(result["errors"])
                self.assertEqual(result["notAttempted"], [])
                self.assertEqual(result["unavailable"], [{"path": batch["items"][-1]["path"], "status": "expired"}])
                self.assertEqual((self.target / "keep").read_bytes(), b"competitor")
                self.assertEqual(list(self.target.iterdir()), [self.target / "keep"])
                for path in paths:
                    self.assertEqual((detached / Path(path).relative_to("/")).read_bytes(), b"fixture")
                json.dumps(result)

    def test_partial_recovery_parent_drift_keeps_previous_success_as_historical_evidence(self):
        batch = self.recovery_batch(held=True, count=3)
        preview = self.preview(batch)
        copy = lifecycle._copy_entry
        detached = self.target.with_name("detached-recovery")
        calls = 0

        def replace(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.target.rename(detached)
                self.target.mkdir()
                (self.target / "keep").write_bytes(b"competitor")
            return copy(*args)

        with patch.object(lifecycle, "_copy_entry", side_effect=replace):
            with self.assertRaises(lifecycle.RecoveryPartialError) as caught:
                self.restore(batch, preview)
        result = caught.exception.result
        paths = [item["path"] for item in batch["items"]]
        self.assertEqual(result["restored"], paths[:1])
        self.assertEqual([item["path"] for item in result["errors"]], paths[1:2])
        self.assertIn(paths[0], [item["path"] for item in result["unknown"]])
        self.assertEqual([item["path"] for item in result["notAttempted"]], paths[2:])
        self.assertEqual((detached / Path(paths[0]).relative_to("/")).read_bytes(), b"fixture")
        self.assertEqual((self.target / "keep").read_bytes(), b"competitor")
        self.assertTrue(all(Path(item["heldPath"]).exists() for item in batch["items"]))

    def test_pre_execution_validation_failure_is_not_partial_recovery(self):
        batch = self.batch()
        preview = self.preview(batch)
        Path(batch["items"][0]["heldPath"]).write_bytes(b"changed")
        with self.assertRaises(StorageError) as caught:
            self.restore(batch, preview)
        self.assertNotIsInstance(caught.exception, lifecycle.RecoveryPartialError)
        self.assertFalse(self.target.exists())

    def test_parent_swap_during_copy_cannot_redirect_write_into_replacement(self):
        batch = self.batch()
        parent = self.root / "parent"
        parent.mkdir()
        self.target = parent / "recovery"
        preview = self.preview(batch)
        copy = lifecycle._copy_entry

        def swap(source, destination_parent, name, check):
            parent.rename(self.root / "detached-parent")
            parent.mkdir()
            (parent / "keep").write_bytes(b"competitor")
            return copy(source, destination_parent, name, check)

        with patch.object(lifecycle, "_copy_entry", side_effect=swap):
            with self.assertRaisesRegex(StorageError, "binding changed"):
                self.restore(batch, preview)
        self.assertEqual(list(parent.iterdir()), [parent / "keep"])
        self.assertEqual((parent / "keep").read_bytes(), b"competitor")

    def test_output_ancestor_replacement_is_detected_before_copy(self):
        batch = self.batch()
        preview = self.preview(batch)
        output = self.target / Path(batch["items"][0]["path"]).relative_to("/")
        copy = lifecycle._copy_entry

        def replace(source, destination_parent, name, check):
            output.parent.rename(output.parent.with_name("detached-output"))
            output.parent.mkdir()
            (output.parent / "keep").write_bytes(b"competitor")
            return copy(source, destination_parent, name, check)

        with patch.object(lifecycle, "_copy_entry", side_effect=replace):
            with self.assertRaisesRegex(StorageError, "output parent directory binding"):
                self.restore(batch, preview)
        self.assertEqual(list(output.parent.iterdir()), [output.parent / "keep"])
        self.assertEqual((output.parent / "keep").read_bytes(), b"competitor")

    def test_approval_cannot_be_reused_for_different_run_or_target(self):
        batch = self.batch()
        preview = self.preview(batch)
        self.target = self.root / "different-target"
        with self.assertRaisesRegex(StorageError, "evidence changed"):
            self.restore(batch, preview)
        self.assertFalse(self.target.exists())
        other = self.batch()
        with self.assertRaisesRegex(StorageError, "evidence changed"):
            self.restore(other, preview)
        self.assertFalse(self.target.exists())


class ArchiveStreamTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        policy = Policy(self.root, "manual", 1, (), (), (), (), "", "")
        self.archive = backend.Archive(policy, self.root / "state")
        self.content = b"fixture remote content"
        self.digest = hashlib.sha256(self.content).hexdigest()
        self.process = MagicMock()
        self.process.pid = 987654
        self.process.stdout = io.BytesIO(self.content)
        self.process.wait.return_value = 0
        self.process.__enter__.return_value = self.process
        self.environment = patch.object(self.archive, "environment", return_value={}).start()
        self.popen = patch.object(backend.subprocess, "Popen", return_value=self.process).start()
        self.kill = patch.object(backend.os, "killpg").start()
        self.timer = patch.object(backend.threading, "Timer").start()
        self.addCleanup(patch.stopall)
        self.path = self.root / "destination"
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        self.addCleanup(os.close, self.fd)

    def restore(self, digest=None):
        self.archive.restore_file("b" * 64, "/fixture/source", self.fd, digest or self.digest)

    def test_dump_stream_writes_and_verifies_fd_without_destination_argument(self):
        self.restore()
        self.assertEqual(self.path.read_bytes(), self.content)
        argv = self.popen.call_args.args[0]
        self.assertEqual(argv, ["restic", "dump", "b" * 64, "/fixture/source"])
        self.assertNotIn(str(self.path), argv)
        self.assertTrue(self.popen.call_args.kwargs["start_new_session"])
        self.timer.assert_called_once()
        self.assertEqual(self.timer.call_args.args[0], 3600)
        self.timer.return_value.cancel.assert_called_once()
        self.process.wait.assert_called_once()

    def test_wrong_digest_and_failed_process_do_not_claim_success(self):
        with self.assertRaisesRegex(StorageError, "did not match"):
            self.restore("0" * 64)
        self.process.stdout = io.BytesIO(self.content)
        self.process.wait.return_value = 255
        os.ftruncate(self.fd, 0)
        os.lseek(self.fd, 0, os.SEEK_SET)
        with self.assertRaisesRegex(StorageError, "verification failed"):
            self.restore()
        self.timer.return_value.cancel.assert_called()

    def test_stream_read_failure_kills_and_reaps_process(self):
        self.process.stdout = MagicMock()
        self.process.stdout.read.side_effect = OSError("fake read error")
        with self.assertRaises(OSError):
            self.restore()
        self.kill.assert_called_once()
        self.process.wait.assert_called_once()
        self.timer.return_value.cancel.assert_called_once()

    def test_timeout_callback_kills_process_and_failed_exit_is_error(self):
        self.process.wait.return_value = -9
        self.timer.return_value.start.side_effect = lambda: self.timer.call_args.args[1]()
        with self.assertRaisesRegex(StorageError, "verification failed"):
            self.restore()
        self.kill.assert_called_once()
        self.process.wait.assert_called_once()

    def test_existing_nonempty_fd_is_rejected_before_native_process(self):
        os.write(self.fd, b"competitor")
        with self.assertRaisesRegex(StorageError, "new exclusive"):
            self.restore()
        self.assertEqual(self.path.read_bytes(), b"competitor")
        self.popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
