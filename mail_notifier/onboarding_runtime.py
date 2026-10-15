"""Lazy process-wide scheduling; worker threads enter the admitted host context.

The ABI slot is shared by profile-specific plugin module imports. Only runnable
jobs enter the executor: queued jobs never occupy a worker while waiting for a
profile lock. Cancelling an await revokes the work's write lease, not just its
asyncio waiter. A thread already inside an external call cannot be killed.
"""

import asyncio
import concurrent.futures
import contextvars
import threading
from collections import deque
from types import SimpleNamespace

from .hermes_host import HostError
from .onboarding_auth import AdmissionReceipt, _home, _shared, owner_key


def _state():
    shared = _shared()
    with shared.lock:
        if not hasattr(shared, "runtime"):
            shared.runtime = SimpleNamespace(profiles={}, queue=deque(), active=0, executor=None,
                                             lease=contextvars.ContextVar("mail_worker_lease", default=None))
        return shared.runtime


def check_active() -> None:
    lease = _state().lease.get()
    if lease is not None and lease.cancelled.is_set():
        raise HostError("setup_work_cancelled")


def bind_operation(draft_id, revision):
    """Bind a slow operation to its checked revision for targeted cancellation."""
    check_active()
    with _shared().lock:
        job = _state().lease.get()
        if job is not None:
            job.reference = (draft_id, revision)


class OnboardingRuntime:
    def __init__(self, *, prune_interval=60):
        self.prune_interval = prune_interval

    def open_profile(self, home):
        """A new registration may reopen a fully drained, unloaded profile."""
        home = _home(home)
        state = _state()
        with _shared().lock:
            profile = state.profiles.get(home)
            if profile is not None and profile.closed and not profile.jobs:
                del state.profiles[home]
            elif profile is not None and profile.closed:
                profile.reopen = True

    def _profile(self, home):
        state = _state()
        if home not in state.profiles:
            state.profiles[home] = SimpleNamespace(closed=False, jobs=[], active=False,
                                                   resource=None, timer=None, receipt=None, reopen=False)
        return state.profiles[home]

    def is_busy(self, home):
        with _shared().lock:
            return self._profile(home).active or _state().active >= 4

    def interrupt(self, receipt, draft_id, revision):
        """An admitted, owner-bound cancel may revoke only that exact operation.

        This changes no draft and claims no cancellation/rollback. The normal
        service cancellation still checks the durable revision after the thread
        returns. A stale button cannot interrupt another revision or owner.
        """
        identity = owner_key(receipt.owner)
        with _shared().lock:
            profile = self._profile(identity[0])
            for job in profile.jobs:
                if owner_key(job.receipt.owner) == identity and job.reference == (draft_id, revision):
                    job.cancelled.set()

    def resource(self, home, factory):
        """Called inside a submitted worker, never during registration/filtering."""
        home = _home(home)
        check_active()
        with _shared().lock:
            profile = self._profile(home)
            if profile.closed:
                raise HostError("setup_work_cancelled")
            if profile.resource is None:
                profile.resource = factory()
            return profile.resource

    async def submit(self, receipt: AdmissionReceipt, work, *, reference=None) -> dict:
        home = owner_key(receipt.owner)[0]
        state = _state()
        with _shared().lock:
            profile = self._profile(home)
            if profile.closed:
                raise HostError("setup_work_cancelled")
            if len(state.queue) >= 128 or len(profile.jobs) >= 50:
                raise HostError("setup_queue_full")
            job = SimpleNamespace(home=home, receipt=receipt, work=work, cancelled=threading.Event(),
                                  result=concurrent.futures.Future(), done=concurrent.futures.Future(), reference=reference)
            profile.jobs.append(job)
            state.queue.append(job)
            profile.receipt = receipt
            if profile.timer is None:
                profile.timer = asyncio.create_task(self._prune(home), name="mail-notifier-prune:" + home)
            self._dispatch()
        future = asyncio.wrap_future(job.result)
        # A cancelled caller must not leave a late worker exception unobserved.
        future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
        try:
            await asyncio.wait([future])
            return future.result()
        except asyncio.CancelledError:
            job.cancelled.set()
            with _shared().lock:
                self._dispatch()
            raise

    def _dispatch(self):
        state = _state()
        for job in list(state.queue):
            profile = state.profiles[job.home]
            if job.cancelled.is_set() or profile.closed:
                state.queue.remove(job)
                profile.jobs.remove(job)
                job.result.set_exception(HostError("setup_work_cancelled"))
                job.done.set_result(None)
            elif state.active < 4 and not profile.active:
                state.queue.remove(job)
                profile.active = True
                state.active += 1
                if state.executor is None:
                    state.executor = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="mail-setup")
                state.executor.submit(self._run, job)

    def _run(self, job):
        state = _state()
        def invoke():
            state.lease.set(job)
            check_active()
            result = job.work()
            check_active()
            return result
        try:
            job.result.set_result(job.receipt.context.copy().run(invoke))
        except BaseException as exc:
            job.result.set_exception(exc)
        finally:
            with _shared().lock:
                profile = state.profiles[job.home]
                profile.jobs.remove(job)
                profile.active = False
                state.active -= 1
                job.done.set_result(None)
                self._dispatch()
                if profile.closed and profile.reopen and not profile.jobs and state.profiles.get(job.home) is profile:
                    del state.profiles[job.home]

    async def _prune(self, home):
        try:
            while True:
                await asyncio.sleep(self.prune_interval)
                with _shared().lock:
                    profile = self._profile(home)
                    receipt = profile.receipt
                    resource = profile.resource
                    if profile.closed:
                        return
                if resource is not None:
                    try:
                        await self.submit(receipt, lambda: resource.prune() or {})
                    except HostError:
                        pass
        except asyncio.CancelledError:
            return

    def revoke_profile(self, home):
        """Synchronous part of unload: revoke leases before returning to the host."""
        home = _home(home)
        with _shared().lock:
            profile = self._profile(home)
            profile.closed = True
            for job in profile.jobs:
                job.cancelled.set()
            self._dispatch()
            resource, profile.resource = profile.resource, None
            timer = profile.timer
        # Do not acquire the service lock while holding the ABI lock: service
        # writes check the lease in the opposite order. Key disposal is in-memory
        # and synchronous even when the gateway loop has already stopped.
        if resource is not None:
            resource.close()
        if timer is not None and not timer.get_loop().is_closed():
            timer.get_loop().call_soon_threadsafe(timer.cancel)
        return profile

    async def close_profile(self, profile_home: str, *, profile=None) -> None:
        profile_home = _home(profile_home)
        # A synchronous unload captures its revoked registration. Its delayed
        # drain must not look up and revoke a newly loaded generation by name.
        if profile is None:
            profile = self.revoke_profile(profile_home)
        with _shared().lock:
            state = _state()
            jobs = list(profile.jobs)
            timer, profile.timer = profile.timer, None
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
            await asyncio.gather(timer, return_exceptions=True)
        await asyncio.gather(*(asyncio.shield(asyncio.wrap_future(job.done)) for job in jobs))
        with _shared().lock:
            resource, profile.resource = profile.resource, None
            profile.receipt = None
            if state.active == 0 and not state.queue and all(p.closed for p in state.profiles.values()):
                executor, state.executor = state.executor, None
                if executor is not None:
                    executor.shutdown(wait=False)
        if resource is not None:
            resource.close()
