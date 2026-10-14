import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class RegistrationTests(unittest.TestCase):
    def load_plugin(self):
        path = Path(__file__).resolve().parents[1] / "__init__.py"
        self.assertTrue(path.exists(), "Native plugin entry point is not implemented")
        name = "hermes_plugin_mail_notifier_test"
        spec = importlib.util.spec_from_file_location(name, path, submodule_search_locations=[str(path.parent)])
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        self.addCleanup(lambda: [sys.modules.pop(k, None) for k in list(sys.modules) if k == name or k.startswith(name + ".")])
        spec.loader.exec_module(module)
        return module

    def test_registration_has_no_network_config_or_storage_io(self):
        module = self.load_plugin()
        ctx = Mock()
        with patch("socket.create_connection", side_effect=AssertionError("network during registration")), patch("sqlite3.connect", side_effect=AssertionError("DB during registration")):
            module.register(ctx)
        ctx.get_config.assert_not_called()
        ctx.set_config.assert_not_called()
        ctx.dispatch_tool.assert_not_called()
        self.assertEqual(ctx.register_cli_command.call_args.kwargs["name"], "mail-notifier")
        names = {call.kwargs["name"] for call in ctx.register_tool.call_args_list}
        self.assertEqual(names, {"mail_notifier_check", "mail_notifier_status", "mail_notifier_history",
                                "mail_notifier_queue", "mail_notifier_feedback", "mail_notifier_policy", "mail_notifier_doctor"})
        manifest = (Path(__file__).resolve().parents[1] / "plugin.yaml").read_text()
        for name in names:
            self.assertIn(name, manifest)

    def test_tool_handlers_always_return_json_including_errors(self):
        module = self.load_plugin()
        ctx = Mock()
        module.register(ctx)
        for call in ctx.register_tool.call_args_list:
            handler = call.kwargs["handler"]
            with patch.object(module, "HermesHost", side_effect=RuntimeError("private message")):
                result = handler({}, task_id="test")
            parsed = json.loads(result)
            self.assertIn("error", parsed)
            self.assertNotIn("private message", result)

    def test_operational_tool_validation_precedes_changes(self):
        from tests.test_cli import SetupHost
        module = self.load_plugin()
        ctx = Mock()
        module.register(ctx)
        handlers = {call.kwargs["name"]: call.kwargs["handler"] for call in ctx.register_tool.call_args_list}
        self.assertIn("mail_notifier_queue", handlers)
        with tempfile.TemporaryDirectory() as path:
            host = SetupHost(path)
            before = host.settings
            cases = [("queue", {"action": "delete"}), ("queue", {"action": "retry"}),
                     ("queue", {"action": "retry", "account_id": "other-profile-account"}),
                     ("queue", {"action": "retarget", "account_id": "a1", "target": "invalid"}),
                     ("feedback", {}), ("feedback", {"action": "remove", "feedback_id": True}),
                     ("policy", {"changes": {"target": "telegram:222"}}), ("doctor", {"live": "false"}),
                     ("history", {"unexpected": True})]
            for name, args in cases:
                with self.subTest(name=name, args=args), patch.object(module, "HermesHost", return_value=host):
                    result = json.loads(handlers["mail_notifier_" + name](args))
                self.assertFalse(result["success"])
                self.assertEqual(host.settings, before)
                self.assertEqual(host.deliveries, [])


if __name__ == "__main__":
    unittest.main()
