"""Fixture isolation regressions. Every probe has an independent pre-egress stop."""

import asyncio
import builtins
import os
import socket
import unittest
from contextlib import ExitStack
from unittest.mock import patch


@unittest.skipUnless(os.environ.get("HERMES_CONTRACT") == "1", "set HERMES_CONTRACT=1 with pinned Hermes")
class GatewayHarnessIsolationTests(unittest.TestCase):
    def setUp(self):
        # These backstops must stay outside the fixture, including during RED:
        # a missing/broken fixture guard must never perform real DNS or connect.
        self.backstops = ExitStack()
        self.addCleanup(self.backstops.close)
        self.escaped = []
        for obj, names in ((socket, ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr",
                                     "getnameinfo", "create_connection")),
                           (socket.socket, ("connect", "connect_ex", "sendto"))):
            for name in names:
                def stop(*args, operation=name, **kwargs):
                    self.escaped.append(operation)
                    raise AssertionError("independent pre-egress stop: " + operation)
                self.backstops.enter_context(patch.object(obj, name, stop))

    def harness(self):
        from tests.telegram_support import GatewayHarness
        harness = GatewayHarness()
        self.addCleanup(lambda: asyncio.run(harness.close()))
        return harness

    def test_async_httpx_is_blocked_before_dns_and_attempt_survives_a_catch(self):
        import httpx
        harness = self.harness()
        async def probe():
            async with httpx.AsyncClient(trust_env=False) as client:
                with self.assertRaisesRegex(AssertionError, "unexpected offline egress: httpx.AsyncClient.send"):
                    await client.get("https://offline-probe.invalid/")
        try:
            asyncio.run(probe())
        finally:
            with self.assertRaisesRegex(AssertionError, "unexpected offline egress"):
                asyncio.run(harness.close())
        self.assertEqual(self.escaped, [])

    def test_resolver_and_connection_entry_points_are_recorded_and_blocked(self):
        harness = self.harness()
        probes = (
            ("socket.getaddrinfo", lambda: socket.getaddrinfo("offline-probe.invalid", 443)),
            ("socket.gethostbyname", lambda: socket.gethostbyname("offline-probe.invalid")),
            ("socket.gethostbyname_ex", lambda: socket.gethostbyname_ex("offline-probe.invalid")),
            ("socket.gethostbyaddr", lambda: socket.gethostbyaddr("192.0.2.1")),
            ("socket.getnameinfo", lambda: socket.getnameinfo(("192.0.2.1", 443), 0)),
            ("socket.create_connection", lambda: socket.create_connection(("offline-probe.invalid", 443))),
        )
        try:
            for operation, probe in probes:
                with self.subTest(operation=operation), self.assertRaisesRegex(AssertionError, operation):
                    probe()
            with socket.socket() as connection:
                for name in ("connect", "connect_ex"):
                    with self.subTest(operation=name), self.assertRaisesRegex(AssertionError, "socket.socket." + name):
                        getattr(connection, name)(("192.0.2.1", 443))
            with socket.socket(type=socket.SOCK_DGRAM) as connection:
                with self.assertRaisesRegex(AssertionError, "socket.socket.sendto"):
                    connection.sendto(b"probe", ("192.0.2.1", 53))
        finally:
            with self.assertRaisesRegex(AssertionError, "unexpected offline egress"):
                asyncio.run(harness.close())
        self.assertEqual(self.escaped, [])

    def test_host_swallowed_http_attempt_still_fails_fixture_cleanup(self):
        import httpx
        harness = self.harness()
        from agent import title_generator
        config = harness.home / "config.yaml"
        config.write_text(config.read_text().replace("enabled: false", "enabled: true"), encoding="utf-8")
        def attempted_rpc(**kwargs):
            return httpx.get("https://offline-probe.invalid/", trust_env=False)
        with patch.object(title_generator, "call_llm", attempted_rpc), self.assertLogs("agent.title_generator", "WARNING") as logs:
            self.assertIsNone(title_generator.generate_title("synthetic fixture probe"))
        self.assertEqual(len(logs.records), 1)
        self.assertIn("Title generation failed:", logs.output[0])
        with self.assertRaisesRegex(AssertionError, "unexpected offline egress: httpx.Client.send"):
            asyncio.run(harness.close())
        self.assertEqual(self.escaped, [])

    def test_guards_cover_first_host_import_plugin_load_and_runner_setup(self):
        from tests.telegram_support import GatewayHarness
        from gateway.run import GatewayRunner
        from hermes_cli.plugins import PluginManager
        original_import = builtins.__import__
        original_load = PluginManager._load_plugin
        original_init = GatewayRunner.__init__
        attempted = []
        def probe(label):
            attempted.append(label)
            try:
                socket.getaddrinfo("offline-probe.invalid", 443)
            except AssertionError:
                pass
        def import_with_probe(name, *args, **kwargs):
            if name == "gateway.config" and not attempted:
                probe("import")
            return original_import(name, *args, **kwargs)
        def load_with_probe(*args, **kwargs):
            probe("load")
            return original_load(*args, **kwargs)
        def init_with_probe(*args, **kwargs):
            probe("runner")
            return original_init(*args, **kwargs)
        with patch.object(builtins, "__import__", import_with_probe), \
             patch.object(PluginManager, "_load_plugin", load_with_probe), \
             patch.object(GatewayRunner, "__init__", init_with_probe):
            harness = GatewayHarness()
        self.addCleanup(lambda: asyncio.run(harness.close()))
        # These are real setup methods, not authorization or routing decisions.
        self.assertEqual(attempted, ["import", "load", "load", "runner"])
        with self.assertRaisesRegex(AssertionError, "unexpected offline egress: socket.getaddrinfo"):
            asyncio.run(harness.close())
        self.assertEqual(self.escaped, [])

    def test_guards_remain_active_during_database_and_log_cleanup(self):
        harness = self.harness()
        import hermes_logging
        attempted = []
        def probe_then_call(label, original):
            def wrapped(*args, **kwargs):
                attempted.append(label)
                try:
                    socket.getaddrinfo("offline-probe.invalid", 443)
                except AssertionError:
                    pass
                return original(*args, **kwargs)
            return wrapped
        store = harness.runner.session_store
        with patch.object(store, "close_all_db_handles", probe_then_call("database", store.close_all_db_handles)), \
             patch.object(hermes_logging, "flush_log_queue", probe_then_call("logs", hermes_logging.flush_log_queue)):
            with self.assertRaisesRegex(AssertionError, "unexpected offline egress: socket.getaddrinfo"):
                asyncio.run(harness.close())
        self.assertEqual(attempted, ["database", "logs"])
        self.assertEqual(self.escaped, [])

    def test_late_constructor_failure_restores_patches_managers_leases_and_environment(self):
        import httpx
        from tests.telegram_support import GatewayHarness
        from run_agent import AIAgent
        from mail_notifier.onboarding_auth import _shared
        import hermes_cli.plugins as plugins
        before_env = dict(os.environ)
        before_managers = dict(plugins._plugin_managers_by_home)
        before_leases = set(_shared().leases)
        before_rpc = AIAgent._interruptible_api_call
        before_connect = socket.socket.connect
        before_resolver = socket.getaddrinfo
        before_async_http = httpx.AsyncClient.send
        before_patch = GatewayHarness.patch
        harness = object.__new__(GatewayHarness)
        def fail_after_patch(instance, obj, name, value):
            before_patch(instance, obj, name, value)
            if name == "_interruptible_streaming_api_call":
                raise RuntimeError("injected late setup failure")
        try:
            with patch.object(GatewayHarness, "patch", fail_after_patch), self.assertRaisesRegex(RuntimeError, "injected late setup failure"):
                harness.__init__()
            self.assertEqual(dict(os.environ), before_env)
            self.assertFalse(harness.root.exists())
            self.assertIs(AIAgent._interruptible_api_call, before_rpc)
            self.assertIs(socket.socket.connect, before_connect)
            self.assertIs(socket.getaddrinfo, before_resolver)
            self.assertIs(httpx.AsyncClient.send, before_async_http)
            self.assertEqual(plugins._plugin_managers_by_home, before_managers)
            self.assertEqual(set(_shared().leases), before_leases)
            for manager in harness.managers:
                self.assertEqual(manager._hooks, {})
                self.assertEqual(manager._plugins, {})
        finally:
            asyncio.run(harness.close())
        self.assertEqual(self.escaped, [])

    def test_runtime_state_is_not_created_inside_the_plugin_source_tree(self):
        from tests.telegram_support import ROOT
        harness = self.harness()
        # Plugin Doctor copies the source tree; a concurrent fixture must not
        # inject changing state.db/log files into that snapshot.
        self.assertFalse(harness.root.is_relative_to(ROOT))

    def test_early_registration_failure_unloads_the_partially_loaded_profile(self):
        from tests.telegram_support import GatewayHarness
        from hermes_cli.plugins import PluginContext
        from mail_notifier.onboarding_auth import _shared
        import hermes_cli.plugins as plugins
        before_env = dict(os.environ)
        before_managers = dict(plugins._plugin_managers_by_home)
        before_leases = set(_shared().leases)
        harness = object.__new__(GatewayHarness)
        original = PluginContext.register_tool
        def register_then_fail(ctx, *args, **kwargs):
            result = original(ctx, *args, **kwargs)
            if kwargs.get("name") == "mail_contract_origin":
                raise RuntimeError("injected registration failure")
            return result
        with patch.object(PluginContext, "register_tool", register_then_fail), self.assertRaisesRegex(RuntimeError, "injected registration failure"):
            harness.__init__()
        self.assertEqual(dict(os.environ), before_env)
        self.assertFalse(harness.root.exists())
        self.assertEqual(plugins._plugin_managers_by_home, before_managers)
        self.assertEqual(set(_shared().leases), before_leases)
        self.assertEqual(harness.managers[0]._hooks, {})
        self.assertEqual(harness.managers[0]._plugins, {})
        asyncio.run(harness.close())  # A second cleanup is safe after setup failed.
        self.assertEqual(self.escaped, [])

    def test_cleanup_error_still_releases_other_resources_and_restores_guards(self):
        import httpx
        before_env = dict(os.environ)
        before_resolver, before_http = socket.getaddrinfo, httpx.AsyncClient.send
        harness = self.harness()
        with patch.object(harness.runner.session_store, "close_all_db_handles", side_effect=RuntimeError("injected close failure")):
            with self.assertRaisesRegex(RuntimeError, "injected close failure"):
                asyncio.run(harness.close())
        self.assertEqual(dict(os.environ), before_env)
        self.assertIs(socket.getaddrinfo, before_resolver)
        self.assertIs(httpx.AsyncClient.send, before_http)
        self.assertFalse(harness.root.exists())
        for manager in harness.managers:
            self.assertEqual(manager._hooks, {})
        self.assertEqual(self.escaped, [])


if __name__ == "__main__":
    unittest.main()
