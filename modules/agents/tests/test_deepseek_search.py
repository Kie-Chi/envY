"""Regression checks for the DeepSeek Search and Fetch MCP server core."""

import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import urllib.error

spec = importlib.util.spec_from_file_location(
    "envy_deepseek_search",
    Path(os.environ.get("ENVY_DEEPSEEK_SEARCH_PATH", str(Path(__file__).parents[1] / "tools/deepseek_search.py"))),
)
server = importlib.util.module_from_spec(spec)
spec.loader.exec_module(server)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def replying(body):
    captured = {}

    def opener(request, timeout):
        captured.update(request=request, timeout=timeout)
        return FakeResponse(body)

    return opener, captured


def failing(error):
    def opener(request, timeout):
        raise error

    return opener


def http_error(status):
    return urllib.error.HTTPError("https://api.deepseek.com", status, "error", {}, io.BytesIO(b"{}"))


class KeyTests(unittest.TestCase):
    def test_reads_and_strips_the_key_file(self):
        with tempfile.NamedTemporaryFile("w") as handle:
            handle.write("sk-test\n")
            handle.flush()
            self.assertEqual(server.read_key({server.KEY_FILE_ENV: handle.name}), "sk-test")

    def test_rejects_missing_unreadable_and_empty_files(self):
        with self.assertRaisesRegex(server.DeepSeekError, "is not set"):
            server.read_key({})
        with self.assertRaisesRegex(server.DeepSeekError, "unreadable"):
            server.read_key({server.KEY_FILE_ENV: "/nonexistent/key"})
        with tempfile.NamedTemporaryFile("w") as handle:
            with self.assertRaisesRegex(server.DeepSeekError, "empty"):
                server.read_key({server.KEY_FILE_ENV: handle.name})


class PayloadTests(unittest.TestCase):
    def test_search_payload_omits_an_absent_question(self):
        self.assertEqual(
            server.search_payload("nix", 5, True),
            {"query": "nix", "count": 5, "need_content": True},
        )
        self.assertEqual(server.search_payload("nix", ori_question="why")["ori_question"], "why")

    def test_search_payload_enforces_documented_bounds(self):
        for query in ("", "   ", "x" * 129):
            with self.assertRaisesRegex(server.DeepSeekError, "query"):
                server.search_payload(query)
        for count in (0, 51):
            with self.assertRaisesRegex(server.DeepSeekError, "count"):
                server.search_payload("nix", count)
        with self.assertRaisesRegex(server.DeepSeekError, "ori_question"):
            server.search_payload("nix", ori_question=" ")

    def test_fetch_payload_requires_a_credential_free_http_url(self):
        self.assertEqual(server.fetch_payload("https://example.com/a"), {"url": "https://example.com/a"})
        for url in (
            "ftp://example.com",
            "https://",
            "not a url",
            "https://user:pass@example.com",
            "https://[bad",
            "https://example.com/" + "x" * 8192,
        ):
            with self.assertRaisesRegex(server.DeepSeekError, "HTTP"):
                server.fetch_payload(url)


class PostTests(unittest.TestCase):
    def test_sends_bearer_auth_and_json(self):
        opener, captured = replying(b'{"results": []}')
        self.assertEqual(server.post("search", {"query": "nix"}, "sk-test", opener), {"results": []})
        request = captured["request"]
        self.assertEqual(request.full_url, "https://api.deepseek.com/v1/tools/search")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-test")
        self.assertEqual(json.loads(request.data), {"query": "nix"})
        self.assertEqual(captured["timeout"], server.TIMEOUT_SECONDS)

    def test_maps_http_failures_without_leaking_the_key(self):
        for status, reason in ((401, "API key"), (402, "balance"), (429, "rate limit"), (503, "service error"), (418, "failed")):
            with self.assertRaises(server.DeepSeekError) as caught:
                server.post("fetch", {}, "sk-secret", failing(http_error(status)))
            message = str(caught.exception)
            self.assertIn(f"HTTP {status}", message)
            self.assertIn(reason, message)
            self.assertNotIn("sk-secret", message)

    def test_reports_network_failures_and_invalid_bodies(self):
        for error in (urllib.error.URLError("down"), TimeoutError()):
            with self.assertRaisesRegex(server.DeepSeekError, "Could not reach"):
                server.post("search", {}, "key", failing(error))
        with self.assertRaisesRegex(server.DeepSeekError, "invalid JSON"):
            server.post("search", {}, "key", replying(b"<html>")[0])
        with self.assertRaisesRegex(server.DeepSeekError, "unexpected"):
            server.post("search", {}, "key", replying(b"[]")[0])


class FormatTests(unittest.TestCase):
    def test_compact_results_keeps_model_fields_only(self):
        result = {"results": [{
            "title": "T", "url": "https://e.com", "summary": "S", "snippet": "drop",
            "site_icon": "drop", "site_name": None, "date": "", "authority": 0,
            "content": "body",
        }]}
        self.assertEqual(
            server.compact_results(result),
            [{"title": "T", "url": "https://e.com", "summary": "S", "authority": 0}],
        )
        self.assertEqual(server.compact_results(result, True)[0]["content"], "body")

    def test_compact_results_requires_a_results_array(self):
        with self.assertRaisesRegex(server.DeepSeekError, "no results"):
            server.compact_results({})

    def test_page_markdown_adds_a_missing_title_once(self):
        self.assertEqual(server.page_markdown({"title": "T", "content": "body"}), "# T\n\nbody")
        self.assertEqual(server.page_markdown({"title": "T", "content": "# T\n\nbody"}), "# T\n\nbody")
        self.assertEqual(server.page_markdown({"title": "", "content": "body"}), "body")

    def test_page_markdown_rejects_empty_content(self):
        for content in ("", "  ", None):
            with self.assertRaisesRegex(server.DeepSeekError, "no usable"):
                server.page_markdown({"title": "T", "content": content})


if __name__ == "__main__":
    unittest.main()
