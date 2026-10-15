import asyncio
import contextvars
import tempfile
import threading
import unittest
from pathlib import Path

from mail_notifier.hermes_host import HostError
from mail_notifier.onboarding_auth import AdmissionReceipt, SetupOwner
from tests.support import feature


class OnboardingRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = feature(self, "mail_notifier.onboarding_runtime")
        self.runtime = self.module.OnboardingRuntime()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.homes = [str(Path(self.temp.name) / str(i)) for i in range(6)]
        self.marker = contextvars.ContextVar("worker_profile", default="wrong")
        self.release = threading.Event()
        self.addCleanup(self.release.set)

    async def asyncTearDown(self):
        if hasattr(self, "runtime"):
            self.release.set()
            for home in self.homes:
                await self.runtime.close_profile(home)

    def receipt(self, home):
        context = contextvars.copy_context()
        context.run(self.marker.set, home)
        return AdmissionReceipt(SetupOwner(home, "telegram", "700", "111", "111"), "session", "42", context)

    async def test_workers_use_receipt_context_and_never_run_on_gateway_thread(self):
        caller = threading.get_ident()
        home = self.homes[0]
        result = await self.runtime.submit(self.receipt(home), lambda: {
            "home": self.marker.get(), "thread": threading.get_ident()})
        self.assertEqual(result["home"], home)
        self.assertNotEqual(result["thread"], caller)

    async def test_actual_execution_is_one_per_profile_and_four_process_wide(self):
        started = asyncio.Queue()
        loop = asyncio.get_running_loop()
        active = set()
        lock = threading.Lock()
        maximum = [0]
        def work(home):
            with lock:
                self.assertNotIn(home, active)
                active.add(home)
                maximum[0] = max(maximum[0], len(active))
            loop.call_soon_threadsafe(started.put_nowait, home)
            self.release.wait(5)
            with lock:
                active.remove(home)
            return {"home": home}
        jobs = [asyncio.create_task(self.runtime.submit(self.receipt(home), lambda h=home: work(h)))
                for home in [self.homes[0], self.homes[0], *self.homes[1:]]]
        first = [await asyncio.wait_for(started.get(), 2) for _ in range(4)]
        self.assertEqual(len(set(first)), 4)
        self.assertTrue(started.empty())
        self.release.set()
        await asyncio.gather(*jobs)
        self.assertEqual(maximum[0], 4)

    async def test_cancelled_running_work_loses_write_lease_and_queued_work_never_starts(self):
        entered = threading.Event()
        writes = []
        def work():
            entered.set()
            self.release.wait(5)
            self.module.check_active()
            writes.append("published")
            return {}
        running = asyncio.create_task(self.runtime.submit(self.receipt(self.homes[0]), work))
        await asyncio.to_thread(entered.wait, 2)
        queued = asyncio.create_task(self.runtime.submit(self.receipt(self.homes[0]), lambda: writes.append("queued") or {}))
        await asyncio.sleep(0)
        queued.cancel()
        running.cancel()
        await asyncio.gather(running, queued, return_exceptions=True)
        self.release.set()
        await self.runtime.close_profile(self.homes[0])
        self.assertEqual(writes, [])

    async def test_unload_is_profile_local_and_disposes_memory_after_running_work(self):
        disposed = []
        class Resource:
            def close(self):
                disposed.append("closed")
        a, b = self.homes[:2]
        await self.runtime.submit(self.receipt(a), lambda: {"resource": self.runtime.resource(a, Resource)})
        await self.runtime.close_profile(a)
        self.assertEqual(disposed, ["closed"])
        with self.assertRaises(HostError):
            await self.runtime.submit(self.receipt(a), lambda: {})
        self.assertEqual(await self.runtime.submit(self.receipt(b), lambda: {"ok": True}), {"ok": True})

    async def test_pruning_runs_in_worker_and_timer_is_removed_on_close(self):
        pruned = asyncio.Event()
        loop = asyncio.get_running_loop()
        caller = threading.get_ident()
        class Resource:
            def prune(inner):
                self.assertNotEqual(threading.get_ident(), caller)
                loop.call_soon_threadsafe(pruned.set)
            def close(inner):
                pass
        runtime = self.module.OnboardingRuntime(prune_interval=.01)
        home = self.homes[0]
        await runtime.submit(self.receipt(home), lambda: {"resource": runtime.resource(home, Resource)})
        await asyncio.wait_for(pruned.wait(), 2)
        await runtime.close_profile(home)
        self.assertFalse([t for t in asyncio.all_tasks() if t.get_name() == "mail-notifier-prune:" + str(Path(home).resolve())])

    async def test_cancelled_commit_rechecks_lease_after_slow_probe_before_publication(self):
        from mail_notifier.onboarding import OnboardingService
        from tests.test_cli import SetupHost
        from tests.test_service import FakeMailbox
        host = SetupHost(self.homes[0])
        host.path.mkdir()
        service = OnboardingService(host, mailbox_factory=lambda a, p: FakeMailbox(a), check_active=self.module.check_active)
        receipt = self.receipt(self.homes[0])
        def prepare():
            d = service.start(receipt.owner)
            d = service.change(d.id, d.owner, d.revision, action="add", values={"provider": "gmail", "username": "me@example.com"})
            return {"draft": service.validate_password(d.id, d.owner, d.revision, "test-only-secret")}
        draft = (await self.runtime.submit(receipt, prepare))["draft"]
        entered = threading.Event()
        original = host.check_environment
        def slow(settings):
            entered.set()
            self.release.wait(5)
            original(settings)
        host.check_environment = slow
        job = asyncio.create_task(self.runtime.submit(receipt, lambda: service.commit(draft.id, draft.owner, draft.revision)))
        await asyncio.to_thread(entered.wait, 2)
        job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        self.release.set()
        await self.runtime.close_profile(self.homes[0])
        self.assertEqual(host.events, [])
        self.assertEqual(host.passwords, {})

    async def test_synchronous_unload_drops_keys_even_without_an_async_close(self):
        disposed = []
        class Resource:
            def close(self):
                disposed.append("private keys dropped")
        home = self.homes[0]
        await self.runtime.submit(self.receipt(home), lambda: {"resource": self.runtime.resource(home, Resource)})
        self.runtime.revoke_profile(home)
        self.assertEqual(disposed, ["private keys dropped"])

    async def test_reloaded_registration_can_submit_after_revoked_worker_drains(self):
        home = self.homes[0]
        entered = threading.Event()
        def slow():
            entered.set()
            self.release.wait(3)
            return {}
        job = asyncio.create_task(self.runtime.submit(self.receipt(home), slow))
        self.assertTrue(await asyncio.to_thread(entered.wait, 2))
        self.runtime.revoke_profile(home)
        self.runtime.open_profile(home)
        self.release.set()
        await asyncio.gather(job, return_exceptions=True)
        result = await self.runtime.submit(self.receipt(home), lambda: {"new registration": True})
        self.assertEqual(result, {"new registration": True})

    async def test_delayed_unload_cleanup_does_not_close_a_new_registration(self):
        home = self.homes[0]
        await self.runtime.submit(self.receipt(home), lambda: {})
        old_profile = self.runtime.revoke_profile(home)
        self.runtime.open_profile(home)
        await self.runtime.submit(self.receipt(home), lambda: {})
        await self.runtime.close_profile(home, profile=old_profile)
        self.assertEqual(await self.runtime.submit(self.receipt(home), lambda: {"still open": True}), {"still open": True})
