import unittest
from unittest.mock import patch

from envy import error
from envy.errno import ErrorCode


class ErrorTests(unittest.TestCase):
    def test_builders_expose_codes_and_actionable_messages(self):
        items = {
            "missing": error.managed_block_missing("machine.nix", hint="Run: envy config refine"),
            "markers": error.managed_block_markers("machine.nix", starts=2, ends=1),
            "syntax": error.syntax("envy.agents.pi.model", "unsupported Nix expression", line=7),
            "duplicate": error.duplicate("envy.agents.pi.model", line=9),
            "object": error.unsupported_object("envy.agentProfiles.fast"),
            "field": error.unknown_field("envy.agents.pi.typo", ("adapter",)),
            "type": error.invalid_type("envy.agents.pi.contextWindow", "a positive integer or null"),
            "value": error.invalid_value(
                "envy.llm.endpoints.x.baseUrl", "expected an http:// or https:// URL"
            ),
            "choice": error.invalid_choice("envy.agents.pi.api", ("openai-completions",)),
            "required": error.missing_required("envy.agentProfiles.fast", "model"),
            "reference": error.unknown_reference(
                "envy.agents.pi.profile", "agent profile", declared="envy.agentProfiles.<name>"
            ),
            "path": error.invalid_path("envy.agents", expected="envy.<namespace>.<name>.<field>"),
        }
        self.assertEqual(items["missing"].code, ErrorCode.MANAGED_BLOCK_MISSING)
        self.assertEqual(items["missing"].hint, "Run: envy config refine")
        self.assertEqual(
            items["markers"].message,
            "managed block markers are malformed (found 2 begin and 1 end marker(s))",
        )
        self.assertEqual(items["syntax"].line, 7)
        self.assertIn("unsupported Nix expression", items["syntax"].message)
        self.assertEqual(items["duplicate"].message, "assigned more than once")
        self.assertEqual(items["object"].code, ErrorCode.UNSUPPORTED_OBJECT)
        self.assertEqual(
            items["object"].hint, "Write one assignment per field instead of a nested attribute set."
        )
        self.assertEqual(items["field"].hint, "Allowed fields: adapter")
        self.assertEqual(items["type"].message, "expected a positive integer or null")
        self.assertEqual(items["value"].message, "expected an http:// or https:// URL")
        self.assertEqual(items["choice"].hint, "Allowed values: openai-completions")
        self.assertEqual(items["required"].message, "missing required field: model")
        self.assertEqual(items["reference"].message, "references an unknown agent profile")
        self.assertEqual(items["reference"].hint, "Declare it under envy.agentProfiles.<name> first.")
        self.assertEqual(items["path"].hint, "Expected envy.<namespace>.<name>.<field>.")

    def test_missing_required_without_a_field_does_not_repeat_the_path(self):
        item = error.missing_required(
            "envy.agents.dsh.adapter", hint="Run: envy agent set envy.agents.dsh.adapter <adapter>"
        )
        self.assertEqual(item.message, "required field is not set")

    def test_report_logs_code_field_line_and_deduplicates_hints(self):
        items = [
            error.unknown_field("envy.agents.a", ("adapter",)),
            error.unknown_field("envy.agents.b", ("adapter",)),
            error.invalid_type("envy.agents.c", "a string"),
        ]
        with patch.object(error.log, "error") as log_error, patch.object(error.log, "hint") as log_hint:
            paths = error.report("agents", items)
        self.assertEqual(paths, ["envy.agents.a", "envy.agents.b", "envy.agents.c"])
        self.assertEqual(log_error.call_count, 3)
        self.assertEqual(
            log_error.call_args_list[2].kwargs,
            {"field": "envy.agents.c", "code": "invalid-type"},
        )
        self.assertEqual(
            log_error.call_args_list[0].kwargs,
            {"field": "envy.agents.a", "code": "unknown-field"},
        )
        self.assertEqual(log_hint.call_count, 1)
        self.assertEqual(log_hint.call_args.args[0], "Allowed fields: adapter")

    def test_report_attaches_line_numbers_only_when_known(self):
        items = [
            error.syntax("envy.agents.a", "unsupported Nix expression", line=12),
            error.invalid_choice("envy.agents.b", ("openai-completions",)),
        ]
        with patch.object(error.log, "error") as log_error, patch.object(error.log, "hint"):
            error.report("agents", items)
        self.assertEqual(log_error.call_args_list[0].kwargs["line"], 12)
        self.assertNotIn("line", log_error.call_args_list[1].kwargs)

    def test_usage_builders_report_cli_targets_and_arguments(self):
        missing = error.not_found("agent profile", "ghost")
        self.assertEqual(missing.code, ErrorCode.NOT_FOUND)
        self.assertEqual(missing.message, "unknown agent profile: ghost")
        existing = error.already_exists("agent profile", "fast", hint="Run: envy agent set")
        self.assertEqual(existing.code, ErrorCode.ALREADY_EXISTS)
        self.assertEqual(existing.message, "agent profile already exists: fast")
        self.assertEqual(existing.hint, "Run: envy agent set")
        invalid = error.invalid_name("profile", "9bad")
        self.assertEqual(invalid.code, ErrorCode.INVALID_ARGUMENT)
        self.assertEqual(invalid.message, "invalid profile name: 9bad")
        self.assertIn("'_'", invalid.hint)
        choice = error.must_be_one_of("api", ("a", "b"))
        self.assertEqual(choice.code, ErrorCode.INVALID_CHOICE)
        self.assertEqual(choice.message, "api must be one of: a, b")
        positive = error.positive_integer("context window")
        self.assertEqual(positive.code, ErrorCode.INVALID_ARGUMENT)
        self.assertEqual(positive.message, "context window must be positive")
        argument = error.invalid_argument("credential values require --stdin", hint="Pipe it.")
        self.assertEqual(argument.code, ErrorCode.INVALID_ARGUMENT)
        self.assertEqual(argument.hint, "Pipe it.")

    def test_summary_supports_prefixes_and_pathless_arguments(self):
        self.assertEqual(
            error.summary([error.not_found("agent", "ghost")]),
            "unknown agent: ghost",
        )
        self.assertEqual(
            error.summary(
                [error.not_found("agent profile", "ghost")],
                prefix="profile cannot be applied",
            ),
            "profile cannot be applied: unknown agent profile: ghost",
        )

    def test_summary_joins_paths_messages_and_hints(self):
        summary = error.summary([
            error.unknown_field("envy.agents.a", ("adapter",)),
            error.invalid_type("envy.agents.b", "a string"),
        ])
        self.assertEqual(
            summary,
            "envy.agents.a: unknown field for this configuration path "
            "(Allowed fields: adapter); envy.agents.b: expected a string",
        )


if __name__ == "__main__":
    unittest.main()
