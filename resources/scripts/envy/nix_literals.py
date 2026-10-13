"""Conservative parser for JSON-compatible Nix literals in managed blocks.

This is not a Nix evaluator. Expressions and interpolation are rejected rather
than guessed, so a managed-block rewrite cannot silently lose their meaning.
"""

import json
import re
from typing import Any

_TOKEN = re.compile(
    r'\s+|\#[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|'
    r'-?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_-]*|[{}\[\]=;.]',
    re.DOTALL,
)


class LiteralError(ValueError):
    pass


class LiteralReader:
    def __init__(self, text: str):
        self.text = text
        self.position = 0

    def peek(self) -> str:
        position = self.position
        token = self.take()
        self.position = position
        return token

    def take(self) -> str:
        while self.position < len(self.text):
            match = _TOKEN.match(self.text, self.position)
            if not match:
                raise LiteralError("unsupported Nix expression")
            self.position = match.end()
            token = match.group()
            if token.isspace() or token.startswith(("#", "/*")):
                continue
            return token
        return ""

    def expect(self, expected: str) -> None:
        if self.take() != expected:
            raise LiteralError(f"expected {expected}")

    def string(self, token: str) -> str:
        # Do not evaluate interpolations, even when they look like JSON strings.
        if re.search(r'(?<!\\)\$\{', token):
            raise LiteralError("Nix interpolation is not supported")
        try:
            return json.loads(token.replace(r'\${', '${'))
        except ValueError:
            raise LiteralError("unsupported Nix string escape") from None

    def value(self) -> Any:
        token = self.take()
        if token == "{":
            result: dict[str, Any] = {}
            while self.peek() != "}":
                key = self.take()
                if key.startswith('"'):
                    key = self.string(key)
                elif not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]*', key):
                    raise LiteralError("expected an attrset key")
                if key in result:
                    raise LiteralError("duplicate attrset key")
                self.expect("=")
                result[key] = self.value()
                self.expect(";")
            self.expect("}")
            return result
        if token == "[":
            result = []
            while self.peek() != "]":
                if not self.peek():
                    raise LiteralError("unterminated list")
                result.append(self.value())
            self.expect("]")
            return result
        if token.startswith('"'):
            return self.string(token)
        if token in {"true", "false", "null"}:
            return {"true": True, "false": False, "null": None}[token]
        if re.fullmatch(r'-?\d+(?:\.\d+)?', token):
            return float(token) if "." in token else int(token)
        if token == "builtins":
            self.expect(".")
            self.expect("fromJSON")
            encoded = self.take()
            if not encoded.startswith('"'):
                raise LiteralError("expected a fromJSON string")
            try:
                return json.loads(self.string(encoded))
            except ValueError:
                raise LiteralError("invalid fromJSON literal") from None
        raise LiteralError("unsupported Nix expression")
