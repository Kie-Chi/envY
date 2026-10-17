"""Regression checks for the local reader and explicit extraction fallback."""

import os
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO

spec = importlib.util.spec_from_file_location(
    "envy_reader", Path(os.environ.get("ENVY_READER_PATH", str(Path(__file__).parents[1] / "skills/read/scripts/read.py")))
)
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)


class ReaderTests(unittest.TestCase):
    def invoke(self, *args):
        output, errors = StringIO(), StringIO()
        with patch("sys.argv", ["envy-read", *args]), redirect_stdout(output), redirect_stderr(errors):
            code = reader.main()
        return code, output.getvalue(), errors.getvalue()

    def test_extract_article(self):
        html = "<html><title>Fixture</title><article><p>" + "Article evidence. " * 30 + "</p></article></html>"
        with patch.object(reader, "fetch", return_value=(html.encode(), "text/html", "utf-8")):
            self.assertIn("Article evidence", reader.local("https://example.com"))

    def test_empty_and_wall(self):
        for text in ("", "Please sign in to continue. " * 30, "Checking your browser " * 30):
            with self.subTest(text=text), self.assertRaises(ValueError):
                reader.validate(text)

    def test_default_never_uses_third_party(self):
        with patch.object(reader, "local", side_effect=ValueError("empty")) as fetch:
            code, _, _ = self.invoke("https://example.com")
        self.assertEqual(code, 1)
        self.assertEqual(fetch.call_count, 1)
        fetch.assert_called_with("https://example.com")

    def test_opt_in_fallback(self):
        with patch.object(reader, "local", side_effect=[ValueError("empty"), "Article " * 30]) as fetch:
            code, text, _ = self.invoke("--use-proxy", "https://example.com")
        self.assertEqual(code, 0)
        self.assertIn("Source: https://example.com", text)
        self.assertEqual(fetch.call_args_list[1].args[0], "https://defuddle.md/https://example.com")

    def test_secret_url_not_in_diagnostics(self):
        with patch.object(reader, "local", side_effect=OSError("https://example.com/?token=secret")):
            _, _, errors = self.invoke("https://example.com/?token=secret")
        self.assertNotIn("token", errors)
        self.assertNotIn("secret", errors)

    def test_unsupported_scheme_and_credentials(self):
        for url in ("file:///etc/passwd", "https://user:password@example.com"):
            with self.subTest(url=url), self.assertRaises(SystemExit):
                self.invoke(url)

    def test_pdf_dispatch(self):
        with patch.object(reader, "fetch", return_value=(b"%PDF-fixture", "application/pdf", "utf-8")), \
             patch.object(reader, "pdf_text", return_value="PDF evidence") as extract:
            self.assertEqual(reader.local("https://example.com/doc"), "PDF evidence")
        extract.assert_called_once_with(b"%PDF-fixture")


if __name__ == "__main__":
    unittest.main()
