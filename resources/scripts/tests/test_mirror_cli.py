"""CLI, cache, and selection flows of `envy mirror` without chsrc, curl, or Nix."""

import json
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from envy import log, main
from envy.mirror import cache as mirror_cache
from envy.mirror import measure as mirror_measure
from envy.mirror import probe as mirror_probe
from envy.mirror import sources as mirror_sources


def setUpModule():
    # Every cache and journal write stays inside a throwaway directory.
    cache = unittest.enterModuleContext(tempfile.TemporaryDirectory())
    state = unittest.enterModuleContext(tempfile.TemporaryDirectory())
    unittest.enterModuleContext(
        patch.dict(os.environ, {"XDG_CACHE_HOME": cache, "XDG_STATE_HOME": state})
    )


def invoke(*args):
    return CliRunner().invoke(main.cli, ["mirror", *args])


def envelope(result):
    # stdout only: a late ResourceWarning from another test may reach stderr.
    return json.loads(result.stdout)


def measurement(code, *, ok, throughput=0, version=None):
    return {
        "code": code,
        "url": f"https://{code}.invalid/",
        "ok": ok,
        "httpStatus": 200 if ok else 0,
        "throughputBps": throughput,
        "detail": "" if ok else "no response",
        "measurementProvider": "curl",
        "measurementParserVersion": version or mirror_measure.MEASUREMENT_PARSER_VERSION,
    }


class MirrorCliTests(unittest.TestCase):
    def setUp(self):
        mirror_cache.MirrorCache().clear()
        self.overrides = {}
        self.written = []

        def start(target, **kwargs):
            patcher = patch(target, **kwargs)
            self.addCleanup(patcher.stop)
            return patcher.start()

        # chsrc is unavailable, so source discovery falls back to the catalog.
        self.chsrc = start("envy.mirror.sources._run_chsrc", return_value=None)
        start("envy.mirror.sources.read_machine_nix", return_value={"envy.mirrors.mode": "china"})
        for module in ("sources", "cli"):
            start(f"envy.mirror.{module}.read_mirror_overrides", side_effect=lambda: dict(self.overrides))
        start("envy.mirror.cli.write_mirror_overrides", side_effect=self.written.append)
        start("envy.mirror.cli.machine_config_file", return_value=Path("/machines/test.nix"))
        self.commit = start("envy.mirror.cli.offer_mutation_commit")

    def test_sources_fall_back_to_the_catalog_and_mark_the_profile_selection(self):
        result = invoke("sources", "npm", "--json")

        self.assertEqual(result.exit_code, 0, result.output)
        data = envelope(result)["data"]
        self.assertEqual(
            [source["code"] for source in data["sources"]],
            [source["code"] for source in mirror_sources.BUILTIN_SOURCES["npm"]],
        )
        self.assertEqual({source["provider"] for source in data["sources"]}, {"catalog"})
        current = [source["code"] for source in data["sources"] if source["current"]]
        self.assertEqual(current, ["npmmirror"])
        self.assertEqual(data["selection"]["origin"], "profile")
        self.assertTrue(mirror_cache.MirrorCache().get("sources", "npm"))

    def test_fresh_source_cache_avoids_chsrc_until_refresh(self):
        mirror_cache.MirrorCache().put(
            "sources", "npm", [{"code": "cached", "url": "https://cached.invalid/"}],
            mirror_sources.SOURCE_CACHE_TTL,
        )
        self.chsrc.side_effect = AssertionError("fresh cache must not run chsrc")

        cached = invoke("sources", "npm", "--json")
        self.assertEqual(cached.exit_code, 0, cached.output)
        self.assertEqual([source["code"] for source in envelope(cached)["data"]["sources"]], ["cached"])

        self.chsrc.side_effect = None
        refreshed = invoke("sources", "npm", "--refresh", "--json")
        self.assertEqual(refreshed.exit_code, 0, refreshed.output)
        self.assertIn("npmmirror", [source["code"] for source in envelope(refreshed)["data"]["sources"]])
        self.assertEqual(self.chsrc.call_count, 1)

    def test_recent_stale_sources_win_over_the_catalog_but_old_ones_do_not(self):
        cache = mirror_cache.MirrorCache()
        cache.put("sources", "npm", [{"code": "recent", "url": "https://recent.invalid/"}], 0)
        self.assertEqual([value["code"] for value in mirror_sources.source_candidates("npm")], ["recent"])

        old = time.time() - mirror_sources.STALE_MAX_AGE - 60
        with patch("envy.mirror.cache.time.time", return_value=old):
            cache.put("sources", "npm", [{"code": "old", "url": "https://old.invalid/"}], 0)
        codes = [value["code"] for value in mirror_sources.source_candidates("npm")]
        self.assertNotIn("old", codes)
        self.assertIn("npmmirror", codes)

    def test_chsrc_rows_replace_the_catalog_and_repair_url_labels(self):
        self.chsrc.return_value = (
            "Available Sources:\n"
            "ali  https://mirrors.aliyun.com/npm/  Aliyun\n"
            "Available Features:\n",
            "",
            0,
        )
        values = mirror_sources.source_candidates("npm", refresh=True)
        self.assertEqual([(value["code"], value["label"]) for value in values], [("ali", "Aliyun")])
        self.assertEqual(
            mirror_sources._normalize_source_labels([{"code": "x", "url": "https://x/", "label": "https://x/ X"}]),
            [{"code": "x", "url": "https://x/", "label": "X"}],
        )

    def test_unknown_targets_and_providers_are_rejected(self):
        self.assertNotEqual(invoke("sources", "maven", "--json").exit_code, 0)
        self.assertNotEqual(invoke("sources", "npm", "--provider", "wget", "--json").exit_code, 0)
        self.assertNotEqual(invoke("measure", "npm", "--provider", "wget", "--json").exit_code, 0)
        self.assertNotEqual(invoke("set", "maven", "x", "--json").exit_code, 0)
        self.assertNotEqual(invoke("reset", "maven", "--json").exit_code, 0)

    def test_sources_show_only_the_selected_providers_measurements(self):
        mirror_cache.MirrorCache().put(
            "measurements", "curl:npm",
            [dict(measurement("npmmirror", ok=True, throughput=4096))],
            mirror_measure.MEASUREMENT_TTL,
        )

        curl = envelope(invoke("sources", "npm", "--provider", "curl", "--json"))["data"]
        measured = next(source for source in curl["sources"] if source["code"] == "npmmirror")
        self.assertEqual(measured["throughputBps"], 4096)
        self.assertFalse(measured["measurementStale"])

        chsrc = envelope(invoke("sources", "npm", "--json"))["data"]
        unmeasured = next(source for source in chsrc["sources"] if source["code"] == "npmmirror")
        self.assertNotIn("throughputBps", unmeasured)

    def test_measure_reuses_a_fresh_cache_and_recommends_the_fastest_source(self):
        results = [
            measurement("npmmirror", ok=True, throughput=100),
            measurement("huawei", ok=True, throughput=900),
            measurement("tencent", ok=False),
        ]
        with patch("envy.mirror.measure._measure_with_curl", return_value=results) as measure:
            first = invoke("measure", "npm", "--provider", "curl", "--json")
            second = invoke("measure", "npm", "--provider", "curl", "--json")

        self.assertEqual(first.exit_code, 0, first.output)
        self.assertEqual(envelope(first)["data"]["cache"], "measured")
        self.assertEqual(envelope(first)["data"]["recommended"]["code"], "huawei")
        self.assertEqual(envelope(first)["data"]["failed"], 1)
        self.assertEqual(envelope(second)["data"]["cache"], "hit")
        measure.assert_called_once()

    def test_failed_measurement_exits_nonzero_and_expires_quickly(self):
        with patch("envy.mirror.measure._measure_with_curl", return_value=[measurement("npmmirror", ok=False)]):
            result = invoke("measure", "npm", "--provider", "curl", "--json")

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIsNone(envelope(result)["data"]["recommended"])
        row = mirror_cache.MirrorCache().get("measurements", "curl:npm")[0]
        self.assertEqual(row["expiresAt"] - row["fetchedAt"], mirror_measure.NEGATIVE_MEASUREMENT_TTL)

    def test_measurements_from_another_parser_version_are_measured_again(self):
        mirror_cache.MirrorCache().put(
            "measurements", "npm",
            [measurement("npmmirror", ok=True, throughput=1, version=mirror_measure.MEASUREMENT_PARSER_VERSION - 1)],
            mirror_measure.MEASUREMENT_TTL,
        )
        self.assertEqual(mirror_measure._fresh_cached_measurements("npm", "chsrc"), [])

        with patch("envy.mirror.measure._measure_with_chsrc", return_value=[measurement("npmmirror", ok=True, throughput=2)]) as measure:
            values = mirror_measure.measured_sources("npm")
        measure.assert_called_once()
        self.assertEqual(values[0]["throughputBps"], 2)

    def test_chsrc_measurement_without_results_marks_every_source_failed(self):
        sources = [{"code": "a", "url": "https://a.invalid/"}, {"code": "b", "url": "https://b.invalid/"}]
        unavailable = mirror_measure._measure_with_chsrc("npm", sources)
        self.assertEqual([value["ok"] for value in unavailable], [False, False])
        self.assertIn("chsrc unavailable", unavailable[0]["detail"])

        self.chsrc.return_value = ("", "network down", 3)
        failed = mirror_measure._measure_with_chsrc("npm", sources)
        self.assertEqual({value["detail"] for value in failed}, {"network down"})

    def test_set_dry_run_previews_without_writing(self):
        result = invoke("set", "npm", "huawei", "--dry-run", "--json")

        self.assertEqual(result.exit_code, 0, result.output)
        plan = envelope(result)["data"]["plan"]
        self.assertEqual(envelope(result)["data"]["result"], "dry-run")
        self.assertEqual(plan["after"]["envy.mirrors.overrides.npm.source"], "chsrc:npm/huawei")
        self.assertTrue(plan["changed"])
        self.assertEqual(self.written, [])
        self.commit.assert_not_called()

    def test_json_mutations_require_explicit_confirmation(self):
        for args in (("set", "npm", "huawei"), ("reset", "npm")):
            with self.subTest(command=args[0]):
                result = invoke(*args, "--json")
                self.assertEqual(result.exit_code, 1, result.output)
                self.assertEqual(envelope(result)["error"]["code"], "confirmation-required")
        self.assertEqual(self.written, [])

    def test_set_replaces_only_the_target_overrides(self):
        self.overrides = {
            "envy.mirrors.overrides.npm.source": "chsrc:npm/tencent",
            "envy.mirrors.overrides.npm.registry": "https://mirrors.cloud.tencent.com/npm/",
            "envy.mirrors.overrides.rust.source": "chsrc:rust/ustc",
        }

        result = invoke("set", "npm", "huawei", "--yes", "--json")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.written, [{
            "envy.mirrors.overrides.rust.source": "chsrc:rust/ustc",
            "envy.mirrors.overrides.npm.source": "chsrc:npm/huawei",
            "envy.mirrors.overrides.npm.registry": "https://mirrors.huaweicloud.com/repository/npm/",
        }])
        self.commit.assert_called_once_with(
            [Path("/machines/test.nix")], "chore(mirror): select npm source huawei", quiet=True,
        )

    def test_set_rejects_a_source_outside_the_candidates(self):
        result = invoke("set", "npm", "nosuch", "--yes", "--json")

        self.assertEqual(result.exit_code, 1, result.output)
        self.assertEqual(envelope(result)["error"]["code"], "source-not-found")
        self.assertEqual(self.written, [])

    def test_reset_removes_only_the_target_overrides(self):
        self.overrides = {
            "envy.mirrors.overrides.npm.source": "chsrc:npm/huawei",
            "envy.mirrors.overrides.go.source": "chsrc:go/goproxy.cn",
        }

        result = invoke("reset", "npm", "--yes", "--json")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.written, [{"envy.mirrors.overrides.go.source": "chsrc:go/goproxy.cn"}])
        self.assertEqual(envelope(result)["data"]["plan"]["after"], {})
        self.commit.assert_called_once_with(
            [Path("/machines/test.nix")], "chore(mirror): reset npm source", quiet=True,
        )

    def test_targets_report_override_and_profile_selections(self):
        self.overrides = {"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"}

        result = invoke("targets", "--json")

        self.assertEqual(result.exit_code, 0, result.output)
        targets = {value["id"]: value for value in envelope(result)["data"]["targets"]}
        self.assertEqual(list(targets), list(mirror_sources.TARGET_SPECS))
        self.assertEqual((targets["npm"]["selectedSource"], targets["npm"]["selectionOrigin"]), ("huawei", "override"))
        self.assertEqual((targets["rust"]["selectedSource"], targets["rust"]["selectionOrigin"]), ("rsproxycn", "profile"))

    def test_unknown_mirror_mode_falls_back_to_the_china_profile(self):
        with patch("envy.mirror.sources.read_machine_nix", return_value={"envy.mirrors.mode": "Martian"}):
            self.assertEqual(mirror_sources.current_mirror_mode(), "china")
        with patch("envy.mirror.sources.read_machine_nix", side_effect=OSError("missing")):
            self.assertEqual(mirror_sources.current_mirror_mode(), "china")
        self.assertEqual(mirror_sources.current_selection("go", mode="upstream", overrides={})["source"], "upstream")

    def test_status_and_probe_read_the_evaluated_policy(self):
        manifest = {
            "id": "test-machine",
            "platform": "darwin",
            "mirrors": {
                "mode": "china",
                "nix": {"substituters": ["https://cache.invalid"]},
                "probes": [
                    {"name": "Nix", "url": "https://cache.invalid/nix-cache-info"},
                    {"name": "PyPI", "url": "https://pypi.invalid/simple"},
                ],
            },
        }
        results = {
            "Nix": mirror_probe.ProbeResult("Nix", "https://cache.invalid/nix-cache-info", True, "200", 12),
            "PyPI": mirror_probe.ProbeResult("PyPI", "https://pypi.invalid/simple", False, "000", None, "timeout"),
        }
        with patch("envy.mirror.cli.machine_manifest", return_value=manifest), patch(
            "envy.mirror.probe.probe_endpoint", side_effect=lambda name, url, timeout: results[name]
        ) as probe:
            status = invoke("status", "--json")
            probed = invoke("probe", "--timeout", "3", "--json")

        self.assertEqual(status.exit_code, 0, status.output)
        self.assertEqual(
            json.loads(status.stdout)["settings"],
            {"mode": "china", "nix.substituters[0]": "https://cache.invalid"},
        )
        self.assertEqual(probed.exit_code, 1, probed.output)
        payload = json.loads(probed.stdout)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual([value["name"] for value in payload["results"]], ["Nix", "PyPI"])
        self.assertEqual(payload["results"][1]["detail"], "timeout")
        self.assertEqual({call.kwargs["timeout"] for call in probe.call_args_list}, {3})

    def test_missing_evaluated_policy_fails_both_policy_commands(self):
        with patch("envy.mirror.cli.machine_manifest", return_value=None):
            self.assertEqual(invoke("status", "--json").exit_code, 1)
            self.assertEqual(invoke("probe", "--json").exit_code, 1)

    def test_probe_without_declared_endpoints_succeeds_with_no_results(self):
        with patch("envy.mirror.cli.machine_manifest", return_value={"id": "m", "mirrors": {"mode": "china"}}):
            result = invoke("probe", "--json")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["results"], [])

    def test_cache_status_and_clean(self):
        mirror_cache.MirrorCache().put("sources", "npm", [{"code": "a"}], mirror_sources.SOURCE_CACHE_TTL)
        mirror_cache.MirrorCache().put("measurements", "npm", [{"code": "a"}], 0)

        stats = envelope(invoke("cache", "status", "--json"))["data"]
        self.assertEqual((stats["sources"], stats["measurements"], stats["stale"]), (1, 1, 1))

        refused = invoke("cache", "clean", "--json")
        self.assertEqual(refused.exit_code, 1)
        self.assertTrue(mirror_cache.mirror_cache_path().exists())

        self.assertTrue(envelope(invoke("cache", "clean", "--yes", "--json"))["data"]["removed"])
        self.assertFalse(envelope(invoke("cache", "clean", "--yes", "--json"))["data"]["removed"])
        self.assertEqual(mirror_cache.MirrorCache().stats()["sources"], 0)

    def test_cache_ignores_corrupt_rows_and_invalid_tables(self):
        cache = mirror_cache.MirrorCache()
        cache.put("sources", "npm", [{"code": "good"}], mirror_sources.SOURCE_CACHE_TTL)
        with closing(sqlite3.connect(cache.path)) as connection, connection:
            connection.execute(
                "INSERT INTO sources VALUES ('npm', 'bad', 'not json', 0, 9999999999)"
            )
            connection.execute(
                "INSERT INTO sources VALUES ('npm', 'list', '[1]', 0, 9999999999)"
            )
        self.assertEqual([value["code"] for value in cache.get("sources", "npm")], ["good"])
        self.assertEqual(cache.get("other", "npm"), [])
        with self.assertRaises(ValueError):
            cache.put("other", "npm", [], 1)

    def test_trust_reports_an_unsupported_platform_without_running_the_helper(self):
        context = SimpleNamespace(mode="china", user="policy-user")
        with patch("envy.mirror.cli.nix_trust_is_applicable", return_value=False), patch(
            "envy.mirror.cli.current_nix_trust_context", return_value=context
        ), patch("envy.mirror.cli.run_nix_trust", side_effect=AssertionError("must not run")):
            result = invoke("trust", "status", "--json")

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(envelope(result)["data"]["state"], "unsupported")

    def test_failed_trust_status_reports_repair_required(self):
        context = SimpleNamespace(mode="china", user="policy-user")
        failed = SimpleNamespace(returncode=1, stdout="", stderr="trust block missing")
        with patch("envy.mirror.cli.nix_trust_is_applicable", return_value=True), patch(
            "envy.mirror.cli.current_nix_trust_context", return_value=context
        ), patch("envy.mirror.cli.run_nix_trust", return_value=failed):
            result = invoke("trust", "status", "--json")

        self.assertEqual(result.exit_code, 1, result.output)
        payload = envelope(result)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], {"code": "repair-required", "message": "trust block missing"})

    def test_source_completion_uses_the_catalog_without_a_cache(self):
        context = SimpleNamespace(params={"target": "NPM"})
        self.assertEqual(mirror_sources.complete_mirror_sources(context, "h"), [("huawei", "Huawei Cloud")])
        self.assertEqual(mirror_sources.complete_mirror_sources(SimpleNamespace(params={}), ""), [])
        self.assertEqual(mirror_sources.complete_mirror_sources(None, ""), [])


POLICY = {
    "id": "test-machine",
    "platform": "darwin",
    "mirrors": {
        "mode": "china",
        "probes": [
            {"name": "Nix", "url": "https://cache.invalid/nix-cache-info"},
            {"name": "PyPI", "url": "https://pypi.invalid/simple"},
        ],
    },
}


class MirrorHumanOutputTests(unittest.TestCase):
    """The table and prompt paths behind each JSON envelope."""

    def setUp(self):
        MirrorCliTests.setUp(self)
        # A wide console keeps table cells on one line.
        patcher = patch.dict(os.environ, {"COLUMNS": "200"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(log.reset_consoles)
        log.reset_consoles()

    def assert_in_order(self, text, *parts):
        positions = [text.index(part) for part in parts]
        self.assertEqual(positions, sorted(positions), text)

    def test_status_targets_and_sources_render_tables(self):
        self.overrides = {"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"}
        with patch("envy.mirror.cli.machine_manifest", return_value=POLICY):
            status = invoke("status")
        targets = invoke("targets")
        sources = invoke("sources", "rust")

        self.assertEqual((status.exit_code, targets.exit_code, sources.exit_code), (0, 0, 0))
        # The title wraps to the width of the narrow table.
        self.assertIn("Mirror policy -", status.output)
        self.assertIn("test-machine", status.output)
        self.assertRegex(status.output, r"│ mode +│ china │")
        self.assertIn("Mirror targets - china profile", targets.output)
        self.assertIn("huawei (override)", targets.output)
        self.assertIn("rsproxycn (profile)", targets.output)
        self.assertIn("Mirror sources - rust (chsrc)", sources.output)
        row = next(line for line in sources.output.splitlines() if "rsproxycn" in line)
        self.assertIn("profile", row)

    def test_measure_table_sorts_results_and_labels_the_cache(self):
        results = [
            measurement("npmmirror", ok=True, throughput=100),
            {**measurement("tencent", ok=False), "httpStatus": None},
            measurement("huawei", ok=True, throughput=900),
        ]
        with patch("envy.mirror.measure._measure_with_curl", return_value=results) as measure:
            measured = invoke("measure", "npm", "-p", "curl")
            cached = invoke("measure", "npm", "-p", "curl")
            refreshed = invoke("measure", "npm", "-p", "curl", "--refresh")

        self.assertEqual(measure.call_count, 2)
        self.assertIn("Mirror measure - npm (curl, measured)", measured.output)
        self.assert_in_order(measured.output, "huawei", "npmmirror", "tencent")
        failed = next(line for line in measured.output.splitlines() if "tencent" in line)
        self.assertIn("FAIL", failed)
        self.assertIn(" - ", failed)
        self.assertIn("(curl, cached)", cached.output)
        self.assertIn("use --refresh to run curl again", cached.output)
        self.assertIn("(curl, refreshed)", refreshed.output)
        self.assertNotIn("use --refresh", refreshed.output)

        with patch("envy.mirror.measure._measure_with_curl", return_value=[measurement("npmmirror", ok=False)]):
            self.assertEqual(invoke("measure", "npm", "-p", "curl", "--refresh").exit_code, 1)

    def test_set_previews_asks_and_writes_only_after_confirmation(self):
        preview = invoke("set", "npm", "huawei", "--dry-run")
        self.assertIn("path=envy.mirrors.overrides.npm.source value=chsrc:npm/huawei", preview.output)

        declined = CliRunner().invoke(main.cli, ["mirror", "set", "npm", "huawei"], input="n\n")
        self.assertEqual(declined.exit_code, 0, declined.output)
        self.assertIn("Write the npm mirror override for huawei?", declined.output)
        self.assertEqual(self.written, [])
        self.commit.assert_not_called()

        accepted = CliRunner().invoke(main.cli, ["mirror", "set", "npm", "huawei"], input="y\n")
        self.assertEqual(accepted.exit_code, 0, accepted.output)
        self.assertEqual(self.written[0]["envy.mirrors.overrides.npm.source"], "chsrc:npm/huawei")
        self.assertIn("mirror override generated", accepted.output)
        self.assertIn("envy plan && envy apply", accepted.output)
        self.commit.assert_called_once_with(
            [Path("/machines/test.nix")], "chore(mirror): select npm source huawei", quiet=False,
        )

    def test_set_and_reset_reject_unknown_names_before_reading_overrides(self):
        for args in (("set", "npm", "nosuch"), ("set", "cobol", "huawei"), ("reset", "cobol")):
            with self.subTest(args=args):
                result = invoke(*args, "--yes")
                self.assertEqual(result.exit_code, 2, result.output)
        self.assertEqual(self.written, [])

    def test_reset_previews_asks_and_writes_only_after_confirmation(self):
        self.overrides = {"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"}

        preview = invoke("reset", "npm", "--dry-run")
        self.assertIn("mirror override reset preview target=npm", preview.output)
        dry = envelope(invoke("reset", "npm", "--dry-run", "--json"))["data"]
        self.assertEqual((dry["result"], dry["plan"]["changed"]), ("dry-run", True))

        declined = CliRunner().invoke(main.cli, ["mirror", "reset", "npm"], input="n\n")
        self.assertEqual((declined.exit_code, self.written), (0, []))

        accepted = CliRunner().invoke(main.cli, ["mirror", "reset", "npm"], input="y\n")
        self.assertEqual(accepted.exit_code, 0, accepted.output)
        self.assertEqual(self.written, [{}])
        self.assertIn("mirror override reset target=npm", accepted.output)
        self.commit.assert_called_once_with(
            [Path("/machines/test.nix")], "chore(mirror): reset npm source", quiet=False,
        )

    def test_cache_status_table_and_confirmed_clean(self):
        mirror_cache.MirrorCache().put("sources", "npm", [{"code": "a"}], mirror_sources.SOURCE_CACHE_TTL)

        status = invoke("cache", "status")
        self.assertIn("Mirror cache", status.output)
        self.assertIn("sources", status.output)

        declined = CliRunner().invoke(main.cli, ["mirror", "cache", "clean"], input="n\n")
        self.assertEqual(declined.exit_code, 0, declined.output)
        self.assertTrue(mirror_cache.mirror_cache_path().exists())

        removed = CliRunner().invoke(main.cli, ["mirror", "cache", "clean"], input="y\n")
        self.assertIn("mirror cache removed", removed.output)
        self.assertFalse(mirror_cache.mirror_cache_path().exists())
        self.assertIn("mirror cache already empty", invoke("cache", "clean", "--yes").output)

    def test_trust_commands_print_helper_output_and_propagate_failures(self):
        context = SimpleNamespace(mode="china", user="policy-user")
        ready = SimpleNamespace(returncode=0, stdout="trusted: yes\n", stderr="note: cached\n")
        broken = SimpleNamespace(returncode=2, stdout="", stderr="")
        with patch("envy.mirror.cli.current_nix_trust_context", return_value=context), patch(
            "envy.mirror.cli.nix_trust_is_applicable", return_value=False
        ):
            self.assertIn("not supported on this platform", invoke("trust", "repair").output)
        with patch("envy.mirror.cli.current_nix_trust_context", return_value=context), patch(
            "envy.mirror.cli.nix_trust_is_applicable", return_value=True
        ), patch("envy.mirror.cli.run_nix_trust", side_effect=[ready, ready, broken]) as helper:
            status = invoke("trust", "status")
            repaired = invoke("trust", "repair", "--json")
            failed = invoke("trust", "repair", "--json")

        self.assertEqual(status.exit_code, 0, status.output)
        self.assertIn("trusted: yes", status.stdout)
        self.assertIn("note: cached", status.stderr)
        self.assertEqual(
            [call.kwargs for call in helper.call_args_list[:2]],
            [
                {"context": context, "capture": True, "quiet": False},
                {"context": context, "capture": False, "quiet": True, "reload": True},
            ],
        )
        self.assertEqual(envelope(repaired)["data"]["state"], "ready")
        self.assertEqual(failed.exit_code, 2)
        self.assertEqual(envelope(failed)["error"], {"code": "error", "message": "error"})

    def test_probe_table_marks_failures_and_logs_details_in_debug_mode(self):
        results = {
            "Nix": mirror_probe.ProbeResult("Nix", "https://cache.invalid/nix-cache-info", True, "200", 12),
            "PyPI": mirror_probe.ProbeResult("PyPI", "https://pypi.invalid/simple", False, "000", None, "timeout"),
        }
        with patch("envy.mirror.cli.machine_manifest", return_value=POLICY), patch(
            "envy.mirror.probe.probe_endpoint", side_effect=lambda name, url, timeout: results[name]
        ), patch.dict(os.environ, {"ENVY_DEBUG": "1"}):
            probed = invoke("probe")

        self.assertEqual(probed.exit_code, 1, probed.output)
        nix = next(line for line in probed.stdout.splitlines() if "Nix " in line)
        pypi = next(line for line in probed.stdout.splitlines() if "PyPI" in line)
        self.assertIn("OK", nix)
        self.assertIn("12 ms", nix)
        self.assertIn("FAIL", pypi)
        self.assertIn(" - ", pypi)
        self.assertIn("one or more endpoints failed failed=1 total=2", probed.stdout)
        self.assertIn("probe detail endpoint=PyPI detail=timeout", probed.stderr)

        with patch("envy.mirror.cli.machine_manifest", return_value={"id": "m", "mirrors": {"mode": "china"}}):
            empty = invoke("probe")
        self.assertEqual(empty.exit_code, 0, empty.output)
        self.assertIn("no probe endpoints are declared", empty.output)


class MirrorCacheConnectionTests(unittest.TestCase):
    """Every cache operation closes its SQLite connection, even on failure."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.cache = mirror_cache.MirrorCache(Path(directory.name) / "index.sqlite3")
        self.opened = []
        connect = sqlite3.connect

        def spy(*args, **kwargs):
            connection = connect(*args, **kwargs)
            self.opened.append(connection)
            return connection

        patcher = patch.object(mirror_cache.sqlite3, "connect", side_effect=spy)
        self.addCleanup(patcher.stop)
        patcher.start()

    def assert_all_closed(self):
        self.assertTrue(self.opened)
        for connection in self.opened:
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_reads_writes_and_stats_close_their_connections(self):
        self.cache.put("sources", "npm", [{"code": "a"}], 60)
        self.assertEqual([value["code"] for value in self.cache.get("sources", "npm")], ["a"])
        self.assertEqual(self.cache.stats()["sources"], 1)

        self.assertEqual(len(self.opened), 3)
        self.assert_all_closed()

    def test_a_corrupt_cache_file_is_ignored_and_closed(self):
        self.cache.path.write_bytes(b"not a database " * 100)

        self.cache.put("sources", "npm", [{"code": "a"}], 60)
        self.assertEqual(self.cache.get("sources", "npm"), [])
        self.assertEqual(self.cache.stats()["sources"], 0)

        self.assertEqual(len(self.opened), 3)
        self.assert_all_closed()


class MirrorOverrideTests(unittest.TestCase):
    def test_python_go_and_rust_overrides_follow_each_ecosystem(self):
        python = mirror_sources._source_overrides("python", {"code": "tuna", "url": "https://pypi.invalid/simple/"})
        go = mirror_sources._source_overrides("go", {"code": "goproxy.cn", "url": "https://goproxy.cn"})
        go_chain = mirror_sources._source_overrides("go", {"code": "chain", "url": "https://a.invalid,https://b.invalid"})
        upstream = mirror_sources._source_overrides("rust", {"code": "upstream", "url": "https://crates.io/"})
        git_index = mirror_sources._source_overrides("rust", {"code": "ustc", "url": "https://mirrors.invalid/crates.io-index"})

        self.assertEqual(python["envy.mirrors.overrides.python.index"], "https://pypi.invalid/simple")
        self.assertEqual(go["envy.mirrors.overrides.go.proxy"], "https://goproxy.cn,direct")
        self.assertEqual(go_chain["envy.mirrors.overrides.go.proxy"], "https://a.invalid,https://b.invalid")
        self.assertEqual(upstream["envy.mirrors.overrides.rust.cargoIndex"], "sparse+https://index.crates.io/")
        self.assertEqual(git_index, {
            "envy.mirrors.overrides.rust.source": "chsrc:rust/ustc",
            "envy.mirrors.overrides.rust.cargoIndex": "https://mirrors.invalid/crates.io-index/",
        })

    def test_rust_curl_probes_use_each_mirrors_artifact_layout(self):
        def urls(code, url):
            return mirror_measure._curl_probe_urls({"code": code, "url": url}, "rust")

        self.assertEqual(urls("upstream", "https://crates.io/"), ["https://static.crates.io/crates/windows/windows-0.62.2.crate"])
        self.assertEqual(
            urls("rsproxycn", "https://rsproxy.cn/index/"),
            ["https://rsproxy.cn/api/v1/crates/windows/windows-0.62.2/download"],
        )
        self.assertEqual(
            urls("ustc", "https://mirrors.invalid/crates.io-index/"),
            ["https://mirrors.invalid/crates/api/v1/crates/windows/windows-0.62.2/download"],
        )
        self.assertEqual(urls("sparse", "https://sparse.invalid/index/"), ["https://sparse.invalid/index/config.json"])
        self.assertEqual(mirror_measure._curl_probe_urls({"code": "none"}, "rust"), [])
        self.assertEqual(mirror_measure._curl_probe_urls({"url": "https://x.invalid"}, None), ["https://x.invalid"])

    def test_curl_measurement_reports_missing_binary_and_timeouts(self):
        source = {"code": "a", "url": "https://a.invalid"}
        with patch("envy.mirror.measure.shutil.which", return_value=None):
            self.assertIn("not available", mirror_measure._measure_one_with_curl(source)["detail"])
        with patch("envy.mirror.measure.shutil.which", return_value="/usr/bin/curl"), patch(
            "envy.mirror.measure.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="curl", timeout=1),
        ):
            self.assertIn("timed out", mirror_measure._measure_one_with_curl(source)["detail"])
        self.assertEqual(mirror_measure._measure_one_with_curl({"code": "empty"})["detail"], "source has no URL")


if __name__ == "__main__":
    unittest.main()
