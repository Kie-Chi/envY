#!/usr/bin/env python3
"""Fetch public web content locally, with explicit third-party fallback."""

import argparse
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from readability import Document
import html2text

WALL_MARKERS = (
    "sign in to continue", "log in to continue", "please sign in",
    "checking your browser", "just a moment...", "登录后查看", "请登录",
    "人机验证", "机器人验证", "subscribe to continue",
)
MAX_BYTES = 10 * 1024 * 1024


def fetch(url):
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 envy-read"})
    with urlopen(request, timeout=30) as response:
        data = response.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            raise ValueError("response exceeds 10 MiB")
        content_type = response.headers.get_content_type()
        charset = response.headers.get_content_charset() or "utf-8"
        return data, content_type, charset


def validate(text):
    if len(text.strip()) < 80:
        raise ValueError("empty or insufficient article content; use browser")
    if any(marker in text.lower() for marker in WALL_MARKERS):
        raise ValueError("login, consent, subscription or bot challenge detected; use browser")
    return text


def pdf_text(data):
    result = subprocess.run(
        ["pdftotext", "-layout", "-", "-"], input=data, capture_output=True,
        timeout=30, check=True,
    )
    return validate(result.stdout.decode("utf-8", errors="replace"))


def local(url):
    data, content_type, charset = fetch(url)
    if content_type == "application/pdf" or data.startswith(b"%PDF-"):
        return pdf_text(data)
    if content_type in ("text/plain", "text/markdown"):
        return validate(data.decode(charset, errors="replace"))
    if content_type not in ("text/html", "application/xhtml+xml"):
        raise ValueError(f"unsupported content type: {content_type}")
    document = Document(data.decode(charset, errors="replace"))
    converter = html2text.HTML2Text()
    converter.body_width = 0
    converter.ignore_images = True
    body = validate(converter.handle(document.summary()))
    return f"# {document.short_title()}\n\n{body}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--use-proxy", action="store_true",
                        help="Allow third-party extraction of a public URL")
    parser.add_argument("source")
    args = parser.parse_args()
    parsed = urlsplit(args.source)
    if not parsed.scheme:
        if args.use_proxy:
            parser.error("third-party extraction requires a public HTTP(S) URL")
        try:
            text = pdf_text(Path(args.source).read_bytes())
            print(text)
            return 0
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"[fetch] tier=pdf status=fail reason={error}", file=sys.stderr)
            return 1
    if parsed.scheme not in ("http", "https") or parsed.username or parsed.password:
        parser.error("only HTTP(S) URLs without embedded credentials are supported")
    attempts = [("local", args.source)]
    if args.use_proxy:
        attempts.extend((name, prefix + args.source) for name, prefix in (
            ("defuddle", "https://defuddle.md/"), ("jina", "https://r.jina.ai/")))
    for tier, url in attempts:
        try:
            text = local(url)
            print(f"[fetch] tier={tier} status=ok", file=sys.stderr)
            print(f"> Source: {args.source}\n\n{text}")
            return 0
        except (OSError, ValueError, LookupError, subprocess.SubprocessError) as error:
            # Do not print request URLs or query parameters in diagnostics.
            # Do not include parser-provided values in diagnostics.
            print(f"[fetch] tier={tier} status=fail reason={type(error).__name__}",
                  file=sys.stderr)
    print("No article extracted; use the browser for dynamic or authenticated pages.",
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
