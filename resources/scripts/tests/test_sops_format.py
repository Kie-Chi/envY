import unittest

from envy.sops_format import content_is_sops_encrypted

METADATA = """\
sops:
  age:
    - recipient: age1fixture
      enc: fixture
  mac: ENC[AES256_GCM,data:fixture]
"""


class SopsFormatTests(unittest.TestCase):
    def test_fully_encrypted_nested_documents_are_accepted(self):
        content = (
            "llm:\n"
            "  steps:\n"
            "    apikey: ENC[AES256_GCM,data:a]\n"
            "  urls:\n"
            "    - ENC[AES256_GCM,data:b]\n"
            "empty: ''\n"
            "unset:\n"
        )
        self.assertTrue(content_is_sops_encrypted(content + METADATA))

    def test_any_plaintext_leaf_is_rejected(self):
        leaves = {
            "string": "apikey: sk-plaintext",
            "number": "port: 42",
            "boolean": "enabled: true",
            "nested": "llm:\n  apikey: sk-plaintext",
            "list item": "urls:\n  - ENC[AES256_GCM,data:a]\n  - https://example.invalid",
        }
        for name, leaf in leaves.items():
            with self.subTest(name):
                self.assertFalse(content_is_sops_encrypted(f"{leaf}\n{METADATA}"))

    def test_only_unencrypted_suffix_keys_may_stay_plaintext(self):
        metadata = METADATA + "  unencrypted_suffix: _unencrypted\n"
        self.assertTrue(content_is_sops_encrypted("url_unencrypted: https://example.invalid\n" + metadata))
        self.assertFalse(content_is_sops_encrypted("url: https://example.invalid\n" + metadata))

    def test_missing_or_malformed_metadata_is_rejected(self):
        cases = {
            "no metadata": "apikey: ENC[AES256_GCM,data:a]\n",
            "plaintext mac": "apikey: ENC[AES256_GCM,data:a]\nsops:\n  age:\n    - recipient: age1\n  mac: '[unencrypted]'\n",
            "missing mac": "apikey: ENC[AES256_GCM,data:a]\nsops:\n  age:\n    - recipient: age1\n",
            "no recipients": "apikey: ENC[AES256_GCM,data:a]\nsops:\n  age: []\n  mac: ENC[AES256_GCM,data:m]\n",
            "not a mapping": "- ENC[AES256_GCM,data:a]\n",
            "invalid yaml": "apikey: [unclosed\n",
            "empty": "",
        }
        for name, content in cases.items():
            with self.subTest(name):
                self.assertFalse(content_is_sops_encrypted(content))


if __name__ == "__main__":
    unittest.main()
