import unittest

from envy.nix_literals import LiteralError, LiteralReader


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


if __name__ == "__main__":
    unittest.main()
