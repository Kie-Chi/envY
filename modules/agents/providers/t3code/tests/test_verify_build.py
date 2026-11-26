import importlib.util
import os
from pathlib import Path
import tempfile
import unittest

module_path = os.environ.get(
    "T3CODE_VERIFY_BUILD", str(Path(__file__).resolve().parents[1] / "verify_build.py")
)
spec = importlib.util.spec_from_file_location("verify_build", module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class PiWidgetBuildTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.assets = self.root / "client" / "assets"
        self.assets.mkdir(parents=True)

    def test_new_client_cannot_hide_an_old_server(self):
        (self.assets / "chat.js").write_text("Goal tasks goal-tasks currentTaskInferred Queued follow-up messages queue-messages queueMessages")
        (self.root / "server.mjs").write_text("old server")
        with self.assertRaisesRegex(ValueError, "Bundled server"):
            module.verify_build(self.root)

    def test_protocol_and_schema_can_use_different_chunks(self):
        (self.assets / "chat.js").write_text("Goal tasks Queued follow-up messages Delivery plan Edit queued follow-up")
        (self.root / "adapter.mjs").write_text("goal-tasks queue-messages provider.queue.control queue-control-result")
        (self.root / "contract.mjs").write_text("currentTaskInferred queueMessages T3CODE_NIX_PACKAGE T3CODE_NIX_VERSION")
        module.verify_build(self.root)

    def test_server_cannot_hide_a_missing_client(self):
        (self.root / "server.mjs").write_text("goal-tasks currentTaskInferred queue-messages queueMessages T3CODE_NIX_PACKAGE T3CODE_NIX_VERSION")
        with self.assertRaisesRegex(ValueError, "Bundled client"):
            module.verify_build(self.root)

    def test_goal_support_cannot_hide_a_missing_queue_server(self):
        (self.assets / "chat.js").write_text("Goal tasks Queued follow-up messages")
        (self.root / "server.mjs").write_text("goal-tasks currentTaskInferred")
        with self.assertRaisesRegex(ValueError, "Bundled server.*queue-messages"):
            module.verify_build(self.root)

    def test_goal_support_cannot_hide_a_missing_queue_client(self):
        (self.assets / "chat.js").write_text("Goal tasks")
        (self.root / "server.mjs").write_text("goal-tasks currentTaskInferred queue-messages queueMessages T3CODE_NIX_PACKAGE T3CODE_NIX_VERSION")
        with self.assertRaisesRegex(ValueError, "Bundled client.*Queued follow-up messages"):
            module.verify_build(self.root)

    def test_read_only_queue_cannot_hide_missing_controls(self):
        (self.assets / "chat.js").write_text("Goal tasks Queued follow-up messages Delivery plan Edit queued follow-up")
        (self.root / "server.mjs").write_text("goal-tasks currentTaskInferred queue-messages queueMessages T3CODE_NIX_PACKAGE T3CODE_NIX_VERSION")
        with self.assertRaisesRegex(ValueError, "queue controls.*provider.queue.control"):
            module.verify_build(self.root)

    def test_widget_support_cannot_hide_missing_nix_service_support(self):
        (self.assets / "chat.js").write_text("Goal tasks Queued follow-up messages")
        (self.root / "server.mjs").write_text("goal-tasks currentTaskInferred queue-messages queueMessages")
        with self.assertRaisesRegex(ValueError, "Nix service.*T3CODE_NIX_PACKAGE"):
            module.verify_build(self.root)


if __name__ == "__main__":
    unittest.main()
