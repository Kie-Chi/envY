import json
import shutil
import subprocess
import unittest

from envy.nix_literals import LiteralError, LiteralReader, escape_string, unescape_string


def read(text: str):
    return LiteralReader(text).value()


class NixLiteralTests(unittest.TestCase):
    def test_json_compatible_literals_are_read(self):
        text = r'''{
          # line comment
          /* block
             comment */
          name = "envy";
          "quoted-key" = "say \"hi\"\n";
          count = -3;
          ratio = 1.5;
          flags = [ true false null ];
          nested = { list = [ "a" { b = 1; } ]; };
        }'''
        self.assertEqual(read(text), {
            "name": "envy",
            "quoted-key": 'say "hi"\n',
            "count": -3,
            "ratio": 1.5,
            "flags": [True, False, None],
            "nested": {"list": ["a", {"b": 1}]},
        })

    def test_escaped_interpolation_is_literal_text(self):
        self.assertEqual(read(r'"cost \${HOME}"'), "cost ${HOME}")

    def test_builtins_from_json_is_decoded(self):
        self.assertEqual(read(r'builtins.fromJSON "{\"a\":[1,2]}"'), {"a": [1, 2]})

    def test_expressions_are_rejected_instead_of_guessed(self):
        cases = {
            "interpolation": '"${pkgs.hello}"',
            "function call": "lib.mkForce true",
            "let": "let x = 1; in x",
            "path": "./file.nix",
            "with": "with pkgs; [ hello ]",
            "operator in attrset": "{ a = 1 + 2; }",
            "duplicate key": "{ a = 1; a = 2; }",
            "unterminated list": "[ 1 2",
            "missing semicolon": "{ a = 1 }",
            "bad fromJSON": 'builtins.fromJSON "{"',
            "bad escape": r'"\q"',
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(LiteralError):
                    read(text)


ESCAPE_CASES = [
    "plain", 'say "hi"', "back\\slash", "trailing\\", "a${b}", "${", "$${x}", "$$${x}",
    "a\\${b}", "cost $5", "end$", "$", "multi\nline\ttab\rreturn", "{}", "ünïcode",
]
# String bodies as a person might write them, decoded the way Nix does.
LITERAL_BODIES = [r"\q\$\n", "$${x}", "$$", "a$b", r"\\", r"\"", "{x}"]
INTERPOLATED_BODIES = ["${x}", "a$$${x}", r"\\${x}", "$a${x}"]


class NixStringEscapeTests(unittest.TestCase):
    def test_escaped_strings_round_trip_on_one_line(self):
        for value in ESCAPE_CASES:
            with self.subTest(value=value):
                body = escape_string(value)
                self.assertEqual(unescape_string(body), value)
                # The agent block reader accepts what the writers produce.
                self.assertEqual(read(f'"{body}"'), value)
                # Line-based readers of the machine block see a single line.
                self.assertNotIn("\n", body)
                self.assertNotIn("\r", body)

    def test_unescape_rejects_interpolation_but_not_doubled_dollars(self):
        self.assertEqual(unescape_string(r"\q\$\n"), "q$\n")
        self.assertEqual(unescape_string("$${x}"), "$${x}")
        for body in INTERPOLATED_BODIES:
            with self.subTest(body=body), self.assertRaisesRegex(LiteralError, "interpolation"):
                unescape_string(body)

    @unittest.skipUnless(shutil.which("nix-instantiate"), "nix-instantiate is not installed")
    def test_nix_agrees_with_escape_and_unescape(self):
        bodies = [escape_string(value) for value in ESCAPE_CASES] + LITERAL_BODIES
        expression = "[ " + " ".join(f'"{body}"' for body in bodies) + " ]"
        result = subprocess.run(
            ["nix-instantiate", "--eval", "--strict", "--json", "--readonly-mode", "--expr", expression],
            capture_output=True, text=True, timeout=120, check=True,
        )
        self.assertEqual(json.loads(result.stdout), ESCAPE_CASES + [unescape_string(body) for body in LITERAL_BODIES])


if __name__ == "__main__":
    unittest.main()
