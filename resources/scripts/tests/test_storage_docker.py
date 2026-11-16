"""Docker storage fixtures never contact a daemon or execute maintenance."""

import copy
import json
import socket
import subprocess
import unittest
from unittest.mock import patch

from envy.storage import docker
from envy.storage.measurement import ScanBudget
from envy.storage.model import StorageError

ENDPOINT = "unix:///tmp/docker-fixture.sock"
OLD = "2020-02-29T11:22:33.123456789Z"
NOW = 1_800_000_000


def disk_fixture():
    return {"LayersSize": 123, "Images": [
        {"Id": "image-old", "Size": 117, "SharedSize": 6,
         "Containers": 0, "Created": 1_500_000_000, "RepoTags": [],
         "Labels": {"com.docker.compose.project": "project-test", "com.docker.compose.service": "web",
                    "dev.envy.amux.image": "1", "private.token": "must-not-appear"}},
        {"Id": "image-tagged", "Size": 6, "SharedSize": 6,
         "Containers": 1, "Created": 1_500_000_000, "RepoTags": ["project:test"]}],
        "BuildCache": [
            {"ID": "cache-old", "Size": 117, "Shared": False, "InUse": False,
             "Type": "regular", "LastUsedAt": OLD},
            {"ID": "cache-shared", "Size": 6, "Shared": True, "InUse": False,
             "Type": "regular", "LastUsedAt": OLD}],
        "Volumes": [{"Name": name, "UsageData": {"Size": 12, "RefCount": 0}}
                    for name in ("amux-home", "amux-nix", "test-finished")],
        "Containers": [{"Id": "container-test-finished", "SizeRw": 5,
                        "ImageID": "image-tagged", "State": "exited", "Mounts": [],
                        "Labels": {"com.docker.compose.project": "project-test", "dev.envy.amux": "1"}}]}


class DockerFixture(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.builder = {"name": "fixture", "driver": "docker",
                        "nodes": [{"name": "fixture", "endpoint": "fixture", "status": "running"}]}
        self.data = disk_fixture()
        self.daemon = "daemon-fixture"
        self.context = "fixture"
        self.endpoint = ENDPOINT
        self.environment = patch.dict("os.environ", {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.command = patch.object(docker, "probe", side_effect=self.probe)
        self.command.start()
        self.addCleanup(self.command.stop)
        self.http = patch.object(docker, "_get", side_effect=self.get)
        self.http.start()
        self.addCleanup(self.http.stop)

    def probe(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        if argv == ["docker", "context", "show"]:
            value = self.context
        elif argv[:3] == ["docker", "context", "inspect"]:
            self.assertEqual(argv[4], docker.CONTEXT_FORMAT)
            self.assertNotIn("Metadata", argv[4])
            value = json.dumps(self.endpoint)
        elif "buildx" in argv and "inspect" in argv:
            self.assertEqual(argv[:3], ["docker", "--host", self.endpoint])
            self.assertNotIn("--bootstrap", argv)
            if "--format" in argv:
                return subprocess.CompletedProcess(argv, 125, "", "unknown flag: --format")
            value = f"Name:          {self.builder['name']}\nDriver:        {self.builder['driver']}\nNodes:\n"
            for node in self.builder["nodes"]:
                value += f"Name:             {node['name']}\nEndpoint:         {node['endpoint']}\nStatus:           {node['status']}\n"
                value += "BuildKit version: v0.29.0\nLabels:\n Name: ignored-nested-label\n"
        else:
            self.fail("Unexpected or mutating Docker command: " + repr(argv))
        return subprocess.CompletedProcess(argv, 0, value, "")

    def get(self, endpoint, path, budget, **kwargs):
        self.assertEqual(endpoint, self.endpoint)
        budget.check()
        return {"ID": self.daemon} if path == "/info" else copy.deepcopy(self.data)


class DockerInspectionTests(DockerFixture):
    def test_117_bytes_plus_6_shared_are_not_123_reclaimable(self):
        with patch.object(docker.time, "time", return_value=NOW):
            result = docker.inspect()
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["buildCache"]["logicalBytes"], 123)
        self.assertEqual(result["buildCache"]["nativeReclaimableBytes"], 117)
        self.assertEqual(result["buildCache"]["policyEligibleBytes"], 117)
        self.assertEqual(result["images"]["logicalBytes"], 123)
        self.assertEqual(result["images"]["nativeReclaimableBytes"], 111)
        self.assertEqual(result["images"]["policyEligibleBytes"], 111)
        self.assertFalse(result["totalsAdditive"])
        self.assertEqual(result["target"]["daemonId"], self.daemon)
        for group in ("images", "buildCache", "volumes", "containers"):
            self.assertTrue(all(item["action"] == "report" for item in result[group]["items"]))

    def test_engine_layers_size_and_unique_size_without_virtual_size(self):
        self.data["LayersSize"] = 3_856_330_517
        self.data["Images"] = [
            {"Id": "image-shared", "Size": 61_972_604, "SharedSize": 61_972_604,
             "Containers": 1, "RepoTags": ["web:latest"]},
            {"Id": "image-partly-shared", "Size": 1_012_523_597, "SharedSize": 1_012_481_477,
             "Containers": 0, "RepoTags": ["project:after"]},
            {"Id": "image-unshared", "Size": 611_041_414, "SharedSize": 0,
             "Containers": 0, "RepoTags": ["project:local"]}]
        result = docker.inspect()
        self.assertEqual(result["images"]["logicalBytes"], 3_856_330_517)
        self.assertEqual([entry["uniqueBytes"] for entry in result["images"]["items"]], [0, 42_120, 611_041_414])
        self.assertEqual(result["images"]["nativeReclaimableBytes"], 611_083_534)
        self.assertEqual(result["images"]["policyEligibleBytes"], 0)
        self.data["Images"][1]["VirtualSize"] = -1
        self.assertEqual(docker.inspect()["images"]["items"][1]["uniqueBytes"], 42_120)

    def test_tagged_images_prove_policy_zero_without_optional_accounting(self):
        for entry in self.data["Images"]:
            entry["RepoTags"] = ["retained:tag"]
            entry.pop("Containers")
            entry.pop("SharedSize")
        result = docker.inspect()
        self.assertEqual(result["images"]["policyEligibleBytes"], 0)
        self.assertIsNone(result["images"]["nativeReclaimableBytes"])

    def test_young_source_local_cache_is_excluded_but_old_unknown_type_stays_unknown(self):
        recent = docker.datetime.fromtimestamp(NOW - 10 * 86400, docker.timezone.utc).isoformat()
        record = {"ID": "source", "Type": "source.local", "Size": 117,
                  "InUse": False, "Shared": False, "LastUsedAt": recent}
        result = docker._group([record], "buildCache", NOW, True, ScanBudget())
        self.assertEqual(result["policyEligibleBytes"], 0)
        record["LastUsedAt"] = OLD
        self.assertIsNone(docker._group([record], "buildCache", NOW, True, ScanBudget())["policyEligibleBytes"])
        for evidence in ({"InUse": True}, {"Shared": True}):
            with self.subTest(evidence=evidence):
                record.update(InUse=False, Shared=False, Size=None, LastUsedAt=None)
                record.update(evidence)
                self.assertEqual(docker._group([record], "buildCache", NOW, True, ScanBudget())["policyEligibleBytes"], 0)
        record.update(InUse=False, Shared=False, LastUsedAt=recent)
        self.assertIsNone(docker._group([record], "buildCache", NOW, False, ScanBudget())["policyEligibleBytes"])

    def test_missing_last_use_is_unknown_and_not_created_time_fallback(self):
        record = self.data["BuildCache"][0]
        record.pop("LastUsedAt")
        record["CreatedAt"] = OLD
        result = docker.inspect()
        self.assertIsNone(result["buildCache"]["policyEligibleBytes"])
        self.assertIsNone(result["buildCache"]["items"][0]["lastUsedAt"])
        self.assertEqual(result["buildCache"]["nativeReclaimableBytes"], 117)

    def test_missing_refs_negative_size_and_boolean_integers_are_unknown(self):
        for field, value in (("Containers", None), ("Containers", True), ("Size", -1),
                             ("Size", True), ("UniqueSize", -1), ("SharedSize", -1)):
            with self.subTest(field=field, value=value):
                self.data = disk_fixture()
                self.data["Images"][0][field] = value
                result = docker.inspect()
                self.assertIsNone(result["images"]["nativeReclaimableBytes"])
                self.assertIsNone(result["images"]["policyEligibleBytes"])
        self.data["Volumes"][0]["UsageData"].pop("RefCount")
        result = docker.inspect()
        self.assertIsNone(result["volumes"]["nativeReclaimableBytes"])
        self.assertIsNone(result["volumes"]["items"][0]["references"])
        self.data["BuildCache"][0].pop("InUse")
        self.assertIsNone(docker.inspect()["buildCache"]["nativeReclaimableBytes"])

    def test_missing_groups_are_null_and_empty_groups_are_zero(self):
        self.data = {"Images": [], "BuildCache": []}
        result = docker.inspect()
        self.assertEqual(result["images"]["count"], 0)
        self.assertIsNone(result["images"]["logicalBytes"])
        self.assertEqual(result["buildCache"]["logicalBytes"], 0)
        self.assertIsNone(result["volumes"]["count"])
        self.assertIsNone(result["containers"]["policyEligibleBytes"])
        self.assertEqual(result["status"], "partial")
        self.data = {}
        result = docker.inspect()
        self.assertEqual(result["status"], "unknown")
        self.assertTrue(result["reason"])

    def test_native_age_and_non_all_exclusions(self):
        for replacement in ({"InUse": True}, {"Shared": True}, {"Type": "internal"},
                            {"Type": "frontend"}, {"LastUsedAt": "2030-01-01T00:00:00Z"}):
            with self.subTest(replacement=replacement), patch.object(docker.time, "time", return_value=NOW):
                self.data = disk_fixture()
                self.data["BuildCache"][0].update(replacement)
                self.assertEqual(docker.inspect()["buildCache"]["policyEligibleBytes"], 0)
        for replacement in ({"Created": NOW}, {"Containers": 1}, {"RepoTags": ["keep:tagged"]}):
            with self.subTest(replacement=replacement), patch.object(docker.time, "time", return_value=NOW):
                self.data = disk_fixture()
                self.data["Images"][0].update(replacement)
                self.assertEqual(docker.inspect()["images"]["policyEligibleBytes"], 0)

    def test_project_and_test_names_never_authorize_deletion(self):
        result = docker.inspect()
        self.assertEqual(result["projects"][0]["project"], "project-test")
        self.assertIsNone(result["projects"][0]["test"])
        self.assertIsNone(result["projects"][0]["completionEvidence"])
        self.assertNotIn("must-not-appear", json.dumps(result))
        self.assertEqual(result["volumes"]["policyEligibleBytes"], 0)
        volumes = result["volumes"]["items"]
        self.assertTrue(volumes[0]["protected"])
        self.assertTrue(volumes[1]["protected"])
        self.assertFalse(volumes[2]["protected"])
        self.assertEqual(result["containers"]["policyEligibleBytes"], 0)
        self.assertIsNone(result["images"]["items"][0]["ownership"]["retentionEvidence"])

    def test_mismatched_builder_keeps_engine_report_but_unknown_cache_policy(self):
        self.builder["driver"] = "docker-container"
        result = docker.inspect()
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["target"]["builder"])
        self.assertIn("native docker driver", result["reason"])
        self.assertIn(result["reason"], result["warnings"][0])
        self.assertEqual(result["buildCache"]["nativeReclaimableBytes"], 117)
        self.assertIsNone(result["buildCache"]["policyEligibleBytes"])
        with self.assertRaises(StorageError):
            docker.maintenance_target()

    def test_entire_inspection_shares_scan_budget(self):
        budget = ScanBudget(max_entries=1)
        result = docker.inspect(budget=budget)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(budget.entries, 1)
        self.assertTrue(all(kwargs["budget"] is budget for _, kwargs in self.calls))
        result = docker.inspect(budget=ScanBudget(seconds=0))
        self.assertEqual(result["status"], "unknown")

    def test_command_failure_timeout_invalid_json_and_socket_failure_are_reports(self):
        for code in (1, 124, 125, 127):
            with self.subTest(code=code), patch.object(docker, "probe", return_value=subprocess.CompletedProcess([], code, "", "sensitive")):
                result = docker.inspect()
                self.assertIn(result["status"], ("unknown", "unavailable"))
                self.assertIsNone(result["images"]["count"])
                self.assertNotIn("sensitive", json.dumps(result))
                with self.assertRaises(StorageError):
                    docker.maintenance_target()
        with patch.object(docker, "probe", return_value=subprocess.CompletedProcess([], 0, "not-json", "")):
            self.assertEqual(docker.inspect()["status"], "unknown")
        with patch.object(docker, "_get", side_effect=ConnectionRefusedError):
            self.assertEqual(docker.inspect()["status"], "unknown")


class DockerAssociationTests(DockerFixture):
    def setUp(self):
        super().setUp()
        self.data["Images"][1]["Containers"] = 1

    def association(self, result, kind, index=0):
        return result[kind]["items"][index]["association"]

    def test_stopped_containers_reference_images_and_named_anonymous_shared_volumes(self):
        mounts = [{"Type": "volume", "Name": name, "Destination": "/data", "RW": True}
                  for name in ("named", "a" * 64)]
        self.data["Containers"][0]["Mounts"] = mounts + [dict(mounts[0], Destination="/again")]
        self.data["Containers"].append({"Id": "second", "ImageID": "image-tagged", "State": "running",
                                         "Mounts": [mounts[0]]})
        self.data["Images"][1]["Containers"] = 2
        self.data["Volumes"] = [{"Name": "named", "UsageData": {"Size": 10, "RefCount": 2}},
                                {"Name": "a" * 64, "UsageData": {"Size": 10, "RefCount": 1}}]
        result = docker.inspect()
        self.assertEqual(self.association(result, "volumes")["referencedBy"], ["container-test-finished", "second"])
        self.assertEqual(self.association(result, "volumes", 1)["referencedBy"], ["container-test-finished"])
        self.assertEqual(self.association(result, "images", 1)["referencedBy"], ["container-test-finished", "second"])
        self.assertEqual(result["containers"]["items"][0]["state"], "exited")
        self.assertEqual(result["volumes"]["associationStatus"], "complete")
        self.assertEqual(result["volumes"]["policyEligibleBytes"], 0)

    def test_bind_projection_never_exposes_source_command_or_status(self):
        raw = self.data["Containers"][0]
        raw.update(Command="secret-command", Status="secret-status")
        raw["Mounts"] = [{"Type": "bind", "Source": "/private/secret", "Destination": "/secret", "RW": True}]
        result = docker.inspect()
        entry = result["containers"]["items"][0]
        self.assertEqual(entry["mounts"], [{"type": "bind"}])
        self.assertEqual(entry["imageId"], "image-tagged")
        self.assertEqual(entry["mountStatus"], "complete")
        self.assertNotIn("secret", json.dumps(result))
        self.assertEqual(self.association(result, "volumes")["referencedBy"], [])

    def test_only_valid_empty_container_inventory_proves_no_references(self):
        self.data["Containers"] = []
        self.data["Images"][1]["Containers"] = 0
        result = docker.inspect()
        for kind in ("images", "volumes"):
            self.assertEqual(result[kind]["associationStatus"], "complete")
            for entry in result[kind]["items"]:
                self.assertEqual(entry["association"]["referencedBy"], [])
        for raw in (None, {}, [None], ["invalid"]):
            with self.subTest(raw=raw):
                self.data["Containers"] = raw
                result = docker.inspect()
                for kind in ("images", "volumes"):
                    self.assertEqual(result[kind]["associationStatus"], "unknown")
                    self.assertIsNone(self.association(result, kind)["referencedBy"])
        self.data.pop("Containers")
        self.assertIsNone(self.association(docker.inspect(), "volumes")["referencedBy"])

    def test_missing_null_and_malformed_mounts_preserve_observed_lower_bound(self):
        good = {"Type": "volume", "Name": "test-finished", "Destination": "/data", "RW": False}
        self.data["Volumes"][2]["UsageData"]["RefCount"] = 1
        for mounts in (None, {}, [None], [{"Type": "volume"}], [good, {"Type": "unknown"}]):
            with self.subTest(mounts=mounts):
                self.data["Containers"][0]["Mounts"] = mounts
                result = docker.inspect()
                association = self.association(result, "volumes", 2)
                self.assertEqual(association["status"], "unknown")
                self.assertIsNone(association["referencedBy"])
                self.assertEqual(association["observedReferencedBy"], ["container-test-finished"] if mounts == [good, {"Type": "unknown"}] else [])
                self.assertEqual(result["containers"]["items"][0]["mountStatus"], "unknown")
        self.data["Containers"][0].pop("Mounts")
        self.assertIsNone(self.association(docker.inspect(), "volumes")["referencedBy"])

    def test_incomplete_mount_metadata_keeps_explicit_volume_name_association(self):
        self.data["Containers"][0]["Mounts"] = [{"Type": "volume", "Name": "test-finished"}]
        self.data["Volumes"][2]["UsageData"]["RefCount"] = 1
        result = docker.inspect()
        self.assertEqual(self.association(result, "volumes", 2)["referencedBy"], ["container-test-finished"])
        mount = result["containers"]["items"][0]
        self.assertEqual(mount["mountStatus"], "unknown")
        self.assertEqual(mount["mounts"], [{"type": "volume", "name": "test-finished", "destination": None, "rw": None}])

    def test_invalid_container_identity_and_image_identity_are_unknown(self):
        for field in ("Id", "ImageID"):
            for value in (None, "", False, [], {}):
                with self.subTest(field=field, value=value):
                    self.data["Containers"][0][field] = value
                    result = docker.inspect()
                    association = self.association(result, "images", 1)
                    self.assertEqual(association["status"], "unknown")
                    self.assertIsNone(association["referencedBy"])
            self.data["Containers"][0][field] = "container-test-finished" if field == "Id" else "image-tagged"
        for field in ("Id", "ImageID"):
            raw = self.data["Containers"][0].pop(field)
            self.assertIsNone(self.association(docker.inspect(), "images", 1)["referencedBy"])
            self.data["Containers"][0][field] = raw

    def test_native_reference_count_mismatch_is_unknown_with_observed_set(self):
        self.data["Images"][1]["Containers"] = 0
        self.data["Containers"][0]["Mounts"] = [{"Type": "volume", "Name": "amux-home", "Destination": "/home", "RW": True}]
        result = docker.inspect()
        for kind, index in (("images", 1), ("volumes", 0)):
            association = self.association(result, kind, index)
            self.assertEqual(association["status"], "unknown")
            self.assertIsNone(association["referencedBy"])
            self.assertEqual(association["observedReferencedBy"], ["container-test-finished"])
            self.assertIn("disagrees", association["reason"])
        self.assertTrue(result["volumes"]["items"][0]["protected"])
        self.data["Volumes"][0]["UsageData"]["RefCount"] = 2
        self.assertEqual(self.association(docker.inspect(), "volumes")["status"], "unknown")

    def test_unknown_source_inventories_and_missing_reference_counts_are_not_complete(self):
        for kind, key in (("images", "Images"), ("volumes", "Volumes")):
            original = self.data[key]
            for raw in (None, {}, [None]):
                with self.subTest(kind=kind, raw=raw):
                    self.data[key] = raw
                    self.assertEqual(docker.inspect()[kind]["associationStatus"], "unknown")
            self.data[key] = original
        self.data["Volumes"][0]["UsageData"].pop("RefCount")
        self.assertIsNone(self.association(docker.inspect(), "volumes")["referencedBy"])
        self.data["Images"][1].pop("Containers")
        self.assertIsNone(self.association(docker.inspect(), "images", 1)["referencedBy"])

    def test_duplicate_and_missing_source_identity_never_prove_complete_association(self):
        for key, identity in (("Images", "Id"), ("Volumes", "Name")):
            original = copy.deepcopy(self.data[key])
            for value in (None, {}, original[1][identity]):
                with self.subTest(key=key, value=value):
                    self.data[key][0][identity] = value
                    result = docker.inspect()
                    kind = "images" if key == "Images" else "volumes"
                    self.assertEqual(result[kind]["associationStatus"], "unknown")
                    self.assertIsNone(self.association(result, kind)["referencedBy"])
            self.data[key] = original
        self.data["Containers"].append(dict(self.data["Containers"][0]))
        result = docker.inspect()
        self.assertIsNone(self.association(result, "images", 1)["referencedBy"])
        self.assertEqual(self.association(result, "images", 1)["observedReferencedBy"], ["container-test-finished"])

    def test_missing_or_invalid_mount_details_do_not_discard_explicit_name(self):
        for details in ({"RW": True}, {"Destination": "/data"}, {"Destination": "/data", "RW": 1}):
            with self.subTest(details=details):
                self.data["Containers"][0]["Mounts"] = [{"Type": "volume", "Name": "test-finished", **details}]
                self.data["Volumes"][2]["UsageData"]["RefCount"] = 1
                result = docker.inspect()
                self.assertEqual(self.association(result, "volumes", 2)["referencedBy"], ["container-test-finished"])
                self.assertEqual(result["containers"]["items"][0]["mountStatus"], "unknown")

    def test_associations_preserve_existing_estimates_and_obey_shared_budget(self):
        report = {kind: docker._group(self.data[key], kind, NOW, True, ScanBudget())
                  for kind, key in (("images", "Images"), ("volumes", "Volumes"), ("containers", "Containers"))}
        before = {kind: {key: report[kind][key] for key in ("nativeReclaimableBytes", "policyEligibleBytes")}
                  for kind in report}
        self.data["Containers"][0]["Mounts"] = [{"Type": "volume", "Name": "amux-home"}]
        docker._associations(report, self.data, ScanBudget())
        self.assertEqual(before, {kind: {key: report[kind][key] for key in before[kind]} for kind in report})
        with self.assertRaises(OSError):
            docker._associations(report, self.data, ScanBudget(max_entries=1))

    def test_malformed_state_does_not_fall_back_to_raw_status(self):
        raw = self.data["Containers"][0]
        raw["Mounts"] = [{"Type": "volume", "Name": "test-finished", "Destination": "/data", "RW": True}]
        self.data["Volumes"][2]["UsageData"]["RefCount"] = 1
        for state in (None, {}, "", False, "Exited (0) a day ago", "missing"):
            with self.subTest(state=state):
                raw.update(State=state, Status="private command output")
                if state == "missing":
                    raw.pop("State")
                result = docker.inspect()
                entry = result["containers"]["items"][0]
                self.assertIsNone(entry["state"])
                self.assertNotIn("private", json.dumps(result))
                for kind in ("images", "volumes", "containers"):
                    self.assertEqual(result[kind]["associationStatus"], "unknown")
                for kind, index in (("images", 1), ("volumes", 2)):
                    association = self.association(result, kind, index)
                    self.assertEqual(association["status"], "unknown")
                    self.assertIsNone(association["referencedBy"])
                    self.assertEqual(association["observedReferencedBy"], ["container-test-finished"])
                    self.assertIn("State", association["reason"])


class DockerTargetTests(DockerFixture):
    def test_native_inspect_text_proves_local_binding_without_format_flag(self):
        result = docker.inspect()
        self.assertEqual(result["status"], "complete")
        self.assertIsNone(result["reason"])
        self.assertEqual(result["target"]["builder"]["nodes"][0]["endpoint"], ENDPOINT)
        commands = [argv for argv, _ in self.calls if "buildx" in argv]
        self.assertEqual(commands, [["docker", "--host", ENDPOINT, "buildx", "inspect", "fixture"]])

    def test_plain_builder_metadata_rejects_ambiguous_identity(self):
        prefix = "Name: fixture\nDriver: docker\nNodes:\n"
        for metadata in (prefix + "Name: one\nEndpoint: fixture\nEndpoint: fixture\nStatus: running\n",
                         prefix + "Endpoint: fixture\nName: one\nStatus: running\n",
                         prefix + "Nodes:\n",
                         "Name: fixture\nName: fixture\nDriver: docker\nNodes:\n"):
            with self.subTest(metadata=metadata), patch.object(docker, "_command", return_value=metadata):
                with self.assertRaisesRegex(StorageError, "ambiguous|duplicate|misplaced"):
                    docker._builder({"context": "fixture", "endpoint": ENDPOINT}, ScanBudget())

    def test_context_environment_precedes_host_and_current(self):
        with patch.dict("os.environ", {"DOCKER_CONTEXT": "fixture", "DOCKER_HOST": "tcp://must-not-contact"}):
            target = docker.maintenance_target()
        self.assertEqual(target["source"], "DOCKER_CONTEXT")
        self.assertEqual(target["endpoint"], ENDPOINT)
        self.assertFalse(any(argv == ["docker", "context", "show"] for argv, _ in self.calls))

    def test_host_environment_precedes_current_context(self):
        self.builder["name"] = "default"
        self.builder["nodes"][0]["endpoint"] = ENDPOINT
        with patch.dict("os.environ", {"DOCKER_HOST": ENDPOINT}):
            target = docker.maintenance_target()
        self.assertEqual(target["source"], "DOCKER_HOST")
        self.assertIsNone(target["context"])
        self.assertFalse(any("context" in argv for argv, _ in self.calls))

    def test_remote_endpoints_are_never_contacted(self):
        for endpoint in ("tcp://user:password@host:2375", "ssh://host", "http://localhost:2375", "npipe:////./pipe/docker_engine"):
            with self.subTest(endpoint=endpoint), patch.dict("os.environ", {"DOCKER_HOST": endpoint}):
                self.calls.clear()
                result = docker.inspect()
                self.assertEqual(result["status"], "remote")
                self.assertNotIn("password", json.dumps(result))
                self.assertEqual(self.calls, [])
                with self.assertRaises(StorageError):
                    docker.maintenance_target()
        self.endpoint = "ssh://secret@host"
        result = docker.inspect()
        self.assertEqual(result["status"], "remote")
        self.assertNotIn("secret", json.dumps(result))

    def test_invalid_local_paths_are_rejected(self):
        for endpoint in ("unix://relative", "unix:relative", "unix:///tmp/../socket", "unix:///socket?x=1", "unix:///socket#frag", "unix:///%2ftmp/socket"):
            with self.subTest(endpoint=endpoint), patch.dict("os.environ", {"DOCKER_HOST": endpoint}):
                with self.assertRaises(StorageError):
                    docker.maintenance_target()

    def test_bound_commands_keep_age_and_native_non_all_semantics(self):
        target = docker.maintenance_target()
        commands = docker.bound_commands(target)
        self.assertEqual(commands, [
            ["docker", "--host", ENDPOINT, "buildx", "prune", "--builder", "fixture", "-f", "--filter", "until=336h"],
            ["docker", "--host", ENDPOINT, "image", "prune", "-f", "--filter", "until=336h"]])
        self.assertTrue(all("prune" not in argv for argv, _ in self.calls))
        selectors = {"DOCKER_CONTEXT": "elsewhere", "DOCKER_HOST": "ssh://remote", "DOCKER_TLS_VERIFY": "1",
                     "DOCKER_TLS": "1", "DOCKER_CERT_PATH": "cert", "DOCKER_API_VERSION": "1.2",
                     "BUILDX_BUILDER": "remote", "BUILDX_CONFIG": "elsewhere", "BUILDKIT_HOST": "tcp://host",
                     "PATH": "/fixture/bin", "DOCKER_CONFIG": "/fixture/config"}
        with patch.dict("os.environ", selectors, clear=True):
            environment = docker.bound_environment()
        self.assertEqual(environment, {"PATH": "/fixture/bin", "DOCKER_CONFIG": "/fixture/config"})

    def test_reverify_detects_context_daemon_endpoint_and_builder_drift(self):
        target = docker.maintenance_target()
        docker.verify_target(target)
        for attribute, value in (("context", "other"), ("daemon", "other-daemon"),
                                 ("endpoint", "unix:///tmp/other.sock")):
            with self.subTest(attribute=attribute):
                original = getattr(self, attribute)
                setattr(self, attribute, value)
                with self.assertRaises(StorageError):
                    docker.verify_target(target)
                setattr(self, attribute, original)
        self.builder["nodes"][0]["name"] = "replacement"
        with self.assertRaises(StorageError):
            docker.verify_target(target)
        with patch.dict("os.environ", {"DOCKER_HOST": "ssh://remote"}):
            with self.assertRaises(StorageError):
                docker.verify_target(target)

    def test_builder_endpoint_driver_multiple_nodes_and_stopped_fail_closed(self):
        original = copy.deepcopy(self.builder)
        cases = [{**original, "driver": "remote"}, {**original, "nodes": []},
                 {**original, "nodes": original["nodes"] * 2},
                 {**original, "nodes": [{"name": "fixture", "endpoint": "unix:///other.sock", "status": "running"}]},
                 {**original, "nodes": [{"name": "fixture", "endpoint": ENDPOINT, "status": "stopped"}]}]
        for builder in cases:
            with self.subTest(builder=builder):
                self.builder = builder
                with self.assertRaises(StorageError):
                    docker.maintenance_target()


class DockerDateTests(unittest.TestCase):
    def test_nanoseconds_timezone_offsets_and_real_calendar_validation(self):
        self.assertIsNotNone(docker._date(OLD))
        self.assertEqual(docker._date("2024-04-01T12:00:00.123456789+02:00"),
                         docker._date("2024-04-01T10:00:00.123456789Z"))
        for value in (None, False, "", "0001-01-01T00:00:00Z", "2023-02-29T00:00:00Z",
                      "2020-01-01T25:00:00Z", "2020-01-01T00:00:00", "2020-01-01T00:00:00.1234567890Z",
                      "2020-01-01T00:00:00+25:00", "2020-01-01T00:00:00+00:99"):
            with self.subTest(value=value):
                self.assertIsNone(docker._date(value))


class FakeSocket:
    def __init__(self, payload, clock=None, tick=0):
        self.payload = payload
        self.clock, self.tick = clock, tick
        self.timeouts = []
        self.requests = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def settimeout(self, value):
        self.timeouts.append(value)

    def connect(self, path):
        if path != "/tmp/docker-fixture.sock":
            raise AssertionError("Unexpected socket path")

    def sendall(self, data):
        self.requests.append(data)

    def recv_into(self, buffer):
        count = min(len(buffer), len(self.payload), 1 if self.tick else len(buffer))
        buffer[:count] = self.payload[:count]
        self.payload = self.payload[count:]
        if self.clock is not None:
            self.clock[0] += self.tick
        return count


def response(body=b'{"ID":"fixture"}', headers=None):
    if headers is None:
        headers = f"Content-Length: {len(body)}\r\n".encode()
    return b"HTTP/1.1 200 OK\r\n" + headers + b"\r\n" + body


class DockerHTTPTests(unittest.TestCase):
    def test_only_local_get_and_small_json_is_parsed(self):
        stream = FakeSocket(response())
        with patch.object(docker.socket, "socket", return_value=stream) as factory:
            result = docker._get(ENDPOINT, "/info", ScanBudget())
        self.assertEqual(result, {"ID": "fixture"})
        factory.assert_called_once_with(socket.AF_UNIX, socket.SOCK_STREAM)
        self.assertTrue(stream.requests[0].startswith(b"GET /info HTTP/1.1\r\n"))
        self.assertTrue(stream.closed)

    def test_declared_and_streamed_oversize_responses_are_rejected(self):
        for payload in (response(headers=b"Content-Length: 9999999\r\n"),
                        response(b"x" * 2048, headers=b""),
                        response(b"x" * 2048, headers=b"Transfer-Encoding: chunked\r\n"),
                        b"HTTP/1.1 200 OK\r\nX-Large: " + b"x" * 2048 + b"\r\n\r\n"):
            with self.subTest(size=len(payload)):
                stream = FakeSocket(payload)
                with patch.object(docker.socket, "socket", return_value=stream):
                    with self.assertRaises(StorageError):
                        docker._get(ENDPOINT, "/system/df", ScanBudget(), max_bytes=256)
                self.assertTrue(stream.closed)

    def test_chunked_response_and_incomplete_content_length(self):
        body = b'{"ID":"fixture"}'
        payload = response(f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n",
                           headers=b"Transfer-Encoding: chunked\r\n")
        with patch.object(docker.socket, "socket", return_value=FakeSocket(payload)):
            self.assertEqual(docker._get(ENDPOINT, "/info", ScanBudget()), {"ID": "fixture"})
        with patch.object(docker.socket, "socket", return_value=FakeSocket(response(b"{}", b"Content-Length: 10\r\n"))):
            with self.assertRaises(StorageError):
                docker._get(ENDPOINT, "/info", ScanBudget())

    def test_slow_headers_and_slow_body_obey_one_whole_deadline(self):
        for prefix in (b"", response(b"", b"Content-Length: 100\r\n")):
            clock = [100.0]
            stream = FakeSocket(response(), clock, tick=0.2)
            # Preload complete headers for the body-only case.
            if prefix:
                original = stream.recv_into

                def first_read(buffer):
                    count = min(len(buffer), len(prefix))
                    buffer[:count] = prefix[:count]
                    stream.recv_into = original
                    return count

                stream.payload = b"x" * 100
                stream.recv_into = first_read
            with self.subTest(body_only=bool(prefix)), patch.object(docker.time, "monotonic", side_effect=lambda: clock[0]):
                budget = ScanBudget(seconds=20)
                with patch.object(docker.socket, "socket", return_value=stream):
                    with self.assertRaises(StorageError):
                        docker._get(ENDPOINT, "/info", budget, timeout=1)
            self.assertLessEqual(clock[0], 101.21)
            self.assertTrue(stream.closed)
            self.assertLess(stream.timeouts[-1], stream.timeouts[0])

    def test_socket_timeout_and_non_object_json_are_unavailable(self):
        with patch.object(docker.socket, "socket", side_effect=TimeoutError):
            with self.assertRaises(StorageError):
                docker._get(ENDPOINT, "/info", ScanBudget())
        for payload in (response(b"[]"), response(b"invalid"), b"HTTP/1.1 500 Error\r\nContent-Length: 0\r\n\r\n"):
            with patch.object(docker.socket, "socket", return_value=FakeSocket(payload)):
                with self.assertRaises(StorageError):
                    docker._get(ENDPOINT, "/info", ScanBudget())


if __name__ == "__main__":
    unittest.main()
