import subprocess
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from typer.testing import CliRunner

from envy import main
from envy.mirror import (
    MirrorCache,
    CHSRC_MEASURE_TIMEOUT,
    _format_throughput,
    _run_chsrc,
    _measure_with_curl,
    _measure_with_chsrc,
    _parse_speed,
    _parse_chsrc_sources,
    _curl_probe_urls,
    _source_overrides,
    annotate_current_selection,
    complete_measurement_providers,
    complete_mirror_sources,
    complete_mirror_targets,
    current_selection,
    mirror_entries,
    probe_endpoint,
    probe_specs,
)


class MirrorTests(unittest.TestCase):
    def test_trust_status_checks_the_daemon_on_both_platforms(self):
        context = SimpleNamespace(mode="china", user="policy-user")
        for platform in ("darwin", "linux"):
            with self.subTest(platform=platform), patch(
                "envy.nix_trust.sys.platform", platform
            ), patch("envy.mirror.current_nix_trust_context", return_value=context), patch(
                "envy.mirror.run_nix_trust",
                return_value=subprocess.CompletedProcess([], 0, "ready", ""),
            ) as run:
                result = CliRunner().invoke(
                    main.cli, ["mirror", "trust", "status", "--json"]
                )

                self.assertEqual(result.exit_code, 0, result.output)
                payload = json.loads(result.output)
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["data"]["state"], "ready")
                self.assertEqual(payload["data"]["platform"], platform)
                run.assert_called_once_with("status", context=context, capture=True, quiet=False)

    def test_entries_flatten_effective_values_and_hide_probe_metadata(self):
        mirrors = {
            "mode": "china",
            "nix": {"substituters": ["https://mirror.invalid", "https://cache.invalid"]},
            "dockerInstallerMirror": None,
            "probes": [{"name": "Nix", "url": "https://mirror.invalid/info"}],
        }

        self.assertEqual(
            list(mirror_entries(mirrors)),
            [
                ("mode", "china"),
                ("dockerInstallerMirror", "disabled"),
                ("nix.substituters[0]", "https://mirror.invalid"),
                ("nix.substituters[1]", "https://cache.invalid"),
            ],
        )

    def test_probe_specs_ignore_malformed_manifest_values(self):
        mirrors = {
            "probes": [
                {"name": "Nix", "url": "https://mirror.invalid/info"},
                {"name": "missing-url"},
                "invalid",
            ]
        }

        self.assertEqual(
            probe_specs(mirrors),
            [("Nix", "https://mirror.invalid/info")],
        )

    @patch("envy.mirror.subprocess.run")
    def test_successful_probe_reports_http_status_and_latency(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "200\t0.1234", "")

        result = probe_endpoint("Nix", "https://mirror.invalid/info", timeout=7)

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "200")
        self.assertEqual(result.elapsed_ms, 123)
        self.assertIn("--head", run.call_args.args[0])
        self.assertIn("7", run.call_args.args[0])

    @patch("envy.mirror.subprocess.run")
    def test_failed_probe_preserves_non_secret_error_detail(self, run):
        run.return_value = subprocess.CompletedProcess([], 28, "000\t5.0", "timeout")

        result = probe_endpoint("Nix", "https://mirror.invalid/info")

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "000")
        self.assertEqual(result.detail, "timeout")

    @patch("envy.mirror._run_chsrc")
    def test_chsrc_source_table_parser_accepts_english_rows(self, run):
        run.return_value = (
            "Available Sources:\n"
            "npmmirror  https://registry.npmmirror.com  npmmirror\n"
            "huawei  https://mirrors.huaweicloud.com/repository/npm/  Huawei Cloud\n"
            "Available Features:\n",
            "",
            0,
        )

        values = _parse_chsrc_sources("npm")

        self.assertEqual([value["code"] for value in values], ["npmmirror", "huawei"])
        self.assertEqual(values[1]["provider"], "chsrc")
        self.assertEqual(values[1]["url"], "https://mirrors.huaweicloud.com/repository/npm/")

    @patch("envy.mirror._run_chsrc")
    def test_chsrc_source_table_keeps_long_url_out_of_the_label(self, run):
        run.return_value = (
            "Available Sources:\n"
            "huawei  Huawei Cloud  https://mirrors.huaweicloud.com/repository/pypi/simple 华为开源镜像站\n"
            "Available Features:\n",
            "",
            0,
        )

        values = _parse_chsrc_sources("python")

        self.assertEqual(values[0]["code"], "huawei")
        self.assertEqual(values[0]["label"], "华为开源镜像站")
        self.assertEqual(
            values[0]["url"],
            "https://mirrors.huaweicloud.com/repository/pypi/simple",
        )

    @patch("envy.mirror._run_chsrc")
    def test_measurement_does_not_trust_chsrc_recommendation_when_all_http_zero(self, run):
        sources = [{"code": "upstream", "label": "Upstream", "url": "https://registry.npmjs.org/"}]
        run.return_value = (
            "1. upstream (https://registry.npmjs.org/) HTTP code 000 0 Byte/s\n"
            "最快镜像站: 上游默认源\n",
            "",
            0,
        )

        values = _measure_with_chsrc("npm", sources)

        self.assertEqual(values[0]["httpStatus"], 0)
        self.assertFalse(values[0]["ok"])
        self.assertEqual(values[0]["detail"], "chsrc reported HTTP 000 / no response")

    def test_speed_parser_accepts_chsrc_byte_units(self):
        self.assertEqual(_parse_speed("17.23 MByte/s"), 17.23 * 1024 ** 2)
        self.assertEqual(_parse_speed("36.52 MB/s"), 36.52 * 1024 ** 2)
        self.assertEqual(_parse_speed("512 KByte/s"), 512 * 1024)

    def test_throughput_formatter_uses_megabytes_per_second(self):
        self.assertEqual(_format_throughput(round(4.85 * 1024 ** 2)), "4.85 MB/s")
        self.assertEqual(_format_throughput(round(80.49 * 1024)), "0.08 MB/s")
        self.assertEqual(_format_throughput(0), "-")

    @patch("envy.mirror.shutil.which", return_value="/usr/bin/chsrc")
    @patch("envy.mirror.subprocess.run")
    def test_chsrc_timeout_preserves_partial_output(self, run, which):
        run.side_effect = subprocess.TimeoutExpired(
            cmd=["/usr/bin/chsrc", "measure", "python"],
            timeout=5,
            output=b"- mirror [precise] ... 4.20 MByte/s\n",
        )

        stdout, stderr, code = _run_chsrc(
            ["measure", "-no-color", "python"],
            timeout=5,
        )

        which.assert_called_once_with("chsrc")
        self.assertIn("4.20 MByte/s", stdout)
        self.assertEqual(code, 124)
        self.assertIn("timed out after 5s", stderr)

    @patch("envy.mirror._run_chsrc")
    def test_partial_chsrc_timeout_keeps_completed_measurements(self, run):
        sources = [
            {"code": "upstream", "label": "Upstream", "url": "https://pypi.org/simple"},
            {"code": "bfsu", "label": "BFSU", "url": "https://mirrors.bfsu.edu.cn/pypi/web/simple"},
        ]
        run.return_value = (
            "^ Upstream (https://pypi.org/simple) [precise] ... 2.40 MByte/s\n",
            "chsrc timed out after 180s",
            124,
        )

        values = _measure_with_chsrc("python", sources)

        self.assertTrue(values[0]["ok"])
        self.assertGreater(values[0]["throughputBps"], 0)
        self.assertFalse(values[1]["ok"])
        self.assertIn("timed out after 180s", values[1]["detail"])

    @patch("envy.mirror._run_chsrc")
    def test_successful_measurement_without_http_suffix_uses_throughput(self, run):
        sources = [{"code": "npmmirror", "label": "npmmirror", "url": "https://registry.npmmirror.com"}]
        run.return_value = (
            "- npmmirror (阿里云赞助) [精准测速] ... 36.52 MByte/s\n"
            "最快镜像站: npmmirror (阿里云赞助)\n"
            "镜像源地址: https://registry.npmmirror.com\n",
            "",
            0,
        )

        values = _measure_with_chsrc("npm", sources)
        run.assert_called_once_with(
            ["measure", "-no-color", "npm"],
            timeout=CHSRC_MEASURE_TIMEOUT,
        )

        self.assertTrue(values[0]["ok"])
        self.assertIsNone(values[0]["httpStatus"])
        self.assertEqual(values[0]["throughputBps"], round(36.52 * 1024 ** 2))

    @patch("envy.mirror.shutil.which", return_value="/usr/bin/curl")
    @patch("envy.mirror.subprocess.run")
    def test_curl_provider_parses_http_and_speed_download(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, "200\t1234.5", "")
        source = {
            "code": "npmmirror",
            "label": "npmmirror",
            "url": "https://registry.npmmirror.com",
        }

        values = _measure_with_curl([source])

        self.assertTrue(values[0]["ok"])
        self.assertEqual(values[0]["measurementProvider"], "curl")
        self.assertEqual(values[0]["httpStatus"], 200)
        self.assertEqual(values[0]["throughputBps"], 1234)
        args = run.call_args.args[0]
        self.assertIn("--write-out", args)
        self.assertIn("--location", args)
        self.assertEqual(args[-1], source["url"])

    @patch("envy.mirror.shutil.which", return_value="/usr/bin/curl")
    @patch("envy.mirror.subprocess.run")
    def test_curl_provider_uses_representative_target_resource(self, run, which):
        run.return_value = subprocess.CompletedProcess([], 0, "206\t2048", "")
        source = {
            "code": "npmmirror",
            "label": "npmmirror",
            "url": "https://registry.npmmirror.com",
        }

        values = _measure_with_curl([source], target="npm")

        probe = "https://registry.npmmirror.com/@tensorflow/tfjs/-/tfjs-4.22.0.tgz"
        self.assertTrue(values[0]["ok"])
        self.assertEqual(values[0]["measurementUrl"], probe)
        args = run.call_args.args[0]
        self.assertIn("--range", args)
        self.assertEqual(args[-1], probe)
        self.assertEqual(_curl_probe_urls(source, "npm"), [probe])

    def test_mirror_completion_is_static_and_target_aware(self):
        self.assertEqual(complete_mirror_targets(None, "n"), [("npm", "NPM")])
        self.assertEqual(
            complete_measurement_providers(None, "c"),
            [("chsrc", "Use chsrc's ecosystem-aware source measurement"),
             ("curl", "Measure each source URL with curl")],
        )

    def test_source_overrides_include_generated_identity_and_target_fields(self):
        npm = _source_overrides("npm", {
            "code": "huawei",
            "url": "https://mirrors.huaweicloud.com/repository/npm/",
        })
        rust = _source_overrides("rust", {
            "code": "rsproxycn",
            "url": "https://rsproxy.cn/index/",
        })

        self.assertEqual(npm["envy.mirrors.overrides.npm.source"], "chsrc:npm/huawei")
        self.assertEqual(npm["envy.mirrors.overrides.npm.registry"], "https://mirrors.huaweicloud.com/repository/npm/")
        self.assertEqual(rust["envy.mirrors.overrides.rust.cargoIndex"], "sparse+https://rsproxy.cn/index/")
        self.assertEqual(rust["envy.mirrors.overrides.rust.distServer"], "https://rsproxy.cn")
        self.assertEqual(rust["envy.mirrors.overrides.rust.updateRoot"], "https://rsproxy.cn/rustup")

    def test_cache_expires_entries_by_table_ttl(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MirrorCache(Path(directory) / "index.sqlite3")
            cache.put("sources", "npm", [{"code": "huawei", "url": "https://example/"}], ttl=0)
            self.assertEqual(cache.get("sources", "npm"), [])
            self.assertEqual(cache.get("sources", "npm", allow_stale=True)[0]["stale"], True)

    def test_profile_and_machine_override_are_exposed_as_current_selection(self):
        profile = current_selection("npm", mode="china", overrides={})
        override = current_selection(
            "npm",
            mode="china",
            overrides={"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"},
        )
        sources, selection = annotate_current_selection(
            "npm",
            [
                {"code": "npmmirror", "url": "https://registry.npmmirror.com"},
                {"code": "huawei", "url": "https://mirror.invalid/npm/"},
            ],
            mode="china",
            overrides={"envy.mirrors.overrides.npm.source": "chsrc:npm/huawei"},
        )

        self.assertEqual(profile["source"], "npmmirror")
        self.assertEqual(profile["origin"], "profile")
        self.assertEqual(override["source"], "huawei")
        self.assertEqual(override["origin"], "override")
        self.assertEqual(selection["profile"], "china")
        self.assertFalse(sources[0]["current"])
        self.assertTrue(sources[1]["current"])
        self.assertEqual(sources[1]["selectionOrigin"], "override")


if __name__ == "__main__":
    unittest.main()
