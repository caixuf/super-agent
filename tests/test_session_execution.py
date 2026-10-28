import asyncio
from collections import defaultdict
from dataclasses import replace

import pytest

from agentcore.session_execution import (
    Claim, Completion, Lease, Message, Request, Result, Route,
    SessionExecution, Snapshot,
)


class TestAuthority:
    __test__ = False

    def __init__(self):
        self.rows = {}
        self.revisions = defaultdict(int)
        self.outputs = []
        self.completions = {}
        self.failure_codes = []
        self.lock = asyncio.Lock()

    async def claim(self, request):
        async with self.lock:
            key = request.session_id, request.request_id
            row = self.rows.get(key)
            if row:
                if row["lease"].request != request:
                    raise ValueError("identity collision")
                return Claim(settled=row.get("result") or Result("duplicate"))
            lease = Lease(request, "generation-1", Snapshot(self.revisions[request.session_id],
                          (Message("user", request.prompt),)))
            self.rows[key] = {"lease": lease, "cancel": False}
            return Claim(lease=lease)

    def current(self, lease):
        row = self.rows[lease.request.session_id, lease.request.request_id]
        return row, row["lease"].generation == lease.generation

    async def commit(self, lease, text):
        async with self.lock:
            row, current = self.current(lease)
            if not current:
                return Result("fenced")
            if row["cancel"]:
                row["result"] = Result("cancelled")
            elif lease.snapshot.revision != self.revisions[lease.request.session_id]:
                row["result"] = Result("needs_rebase")
            else:
                self.revisions[lease.request.session_id] += 1
                self.outputs.append((lease.request, text))
                row["result"] = Result("accepted", text)
            return row["result"]

    async def fail(self, lease, reason):
        self.failure_codes.append(reason)
        row, current = self.current(lease)
        if not current:
            return Result("fenced")
        row["result"] = Result("failed")
        return row["result"]

    async def request_cancel(self, request):
        row = self.rows.get((request.session_id, request.request_id))
        if not row or row["lease"].request != request or row.get("result"):
            return False
        row["cancel"] = True
        return True

    async def cancelled(self, lease):
        row, current = self.current(lease)
        if not current:
            return Result("fenced")
        row["result"] = Result("cancelled")
        return row["result"]

    async def record_completion(self, completion):
        key = completion.owner_session_id, completion.task_id
        row = self.rows.get(key)
        if not row or row["lease"].request.route != completion.route:
            raise PermissionError("task owner/route mismatch")
        old = self.completions.get(completion.event_id)
        if old and old != completion:
            raise ValueError("completion identity collision")
        self.completions[completion.event_id] = completion
        return Result("accepted", completion.summary)


class Client:
    def __init__(self, block=False):
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not block:
            self.release.set()
        self.closed = False

    async def execute(self, request, snapshot, cancelled):
        self.started.set()
        await self.release.wait()
        return request.prompt + " answer"

    async def aclose(self):
        self.closed = True


class Factory:
    def __init__(self):
        self.clients = {}

    async def open(self, lease):
        client = Client(lease.request.prompt == "slow")
        self.clients[lease.request.request_id] = client
        return client


def request(rid="one", session="session-a", prompt="slow"):
    return Request(session, rid, Route("profile", "channel", "user"), prompt)


async def started(factory, rid):
    for _ in range(100):
        if rid in factory.clients:
            await factory.clients[rid].started.wait()
            return factory.clients[rid]
        await asyncio.sleep(0)
    raise AssertionError("executor was not started")


def test_same_session_models_run_without_holding_session_lock_and_stale_result_is_explicit():
    async def run():
        store, factory = TestAuthority(), Factory()
        runtime = SessionExecution(store, factory)
        slow = asyncio.create_task(runtime.run(request()))
        client = await started(factory, "one")
        quick = await asyncio.wait_for(runtime.run(request("two", prompt="quick")), 1)
        assert quick == Result("accepted", "quick answer")
        assert not slow.done() and not client.closed
        client.release.set()
        assert (await slow).status == "needs_rebase"
        assert [req.request_id for req, _ in store.outputs] == ["two"]
        assert all(client.closed for client in factory.clients.values())
    asyncio.run(run())


def test_request_cancel_does_not_close_another_session_client():
    async def run():
        store, factory = TestAuthority(), Factory()
        runtime = SessionExecution(store, factory)
        first, second = request(), request("two", "session-b")
        a, b = asyncio.create_task(runtime.run(first)), asyncio.create_task(runtime.run(second))
        ca, cb = await started(factory, "one"), await started(factory, "two")
        assert await runtime.cancel(first)
        assert (await a).status == "cancelled" and ca.closed
        assert not b.done() and not cb.closed
        cb.release.set()
        assert (await b).status == "accepted"
        assert not await runtime.cancel(first)
    asyncio.run(run())


def test_disconnected_viewer_does_not_cancel_accepted_execution():
    async def run():
        store, factory = TestAuthority(), Factory()
        runtime = SessionExecution(store, factory)
        req = request()
        viewer = asyncio.create_task(runtime.run(req))
        client = await started(factory, "one")
        viewer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await viewer
        assert not client.closed
        second_viewer = asyncio.create_task(runtime.run(req))
        client.release.set()
        assert (await second_viewer).status == "accepted"
        assert len(factory.clients) == 1 and len(store.outputs) == 1
        assert (await runtime.run(req)).status == "accepted"
        assert len(store.outputs) == 1
    asyncio.run(run())


def test_completion_bypasses_model_capacity_and_cannot_borrow_foreground_route():
    async def run():
        store, factory = TestAuthority(), Factory()
        runtime = SessionExecution(store, factory, capacity=1)
        req = request()
        running = asyncio.create_task(runtime.run(req))
        client = await started(factory, "one")
        completion = Completion("event", "one", "session-a", req.route, "report ready")
        assert (await asyncio.wait_for(runtime.background_completed(completion), 1)).status == "accepted"
        assert (await runtime.background_completed(completion)).status == "accepted"
        assert len(store.completions) == 1 and len(factory.clients) == 1 and not running.done()
        with pytest.raises(PermissionError):
            await runtime.background_completed(replace(completion, route=replace(req.route, user="other")))
        client.release.set()
        assert (await running).status == "accepted"
    asyncio.run(run())


def test_old_generation_cannot_commit_after_replacement():
    async def run():
        store, factory = TestAuthority(), Factory()
        runtime = SessionExecution(store, factory)
        work = asyncio.create_task(runtime.run(request()))
        client = await started(factory, "one")
        row = store.rows["session-a", "one"]
        row["lease"] = replace(row["lease"], generation="generation-2")
        client.release.set()
        assert (await work).status == "fenced" and not store.outputs
    asyncio.run(run())


def test_factory_cannot_share_live_executor_or_close_its_other_owner():
    async def run():
        store = TestAuthority()
        client = Client(block=True)
        class BadFactory:
            async def open(self, lease):
                return client
        runtime = SessionExecution(store, BadFactory())
        first = asyncio.create_task(runtime.run(request()))
        await client.started.wait()
        second = await runtime.run(request("two", "session-b"))
        assert second.status == "failed" and store.failure_codes == ["SharedExecutorError"]
        assert not client.closed and not first.done()
        client.release.set()
        assert (await first).status == "accepted"
    asyncio.run(run())


def test_identity_reuse_with_different_prompt_is_rejected():
    async def run():
        store, factory = TestAuthority(), Factory()
        runtime = SessionExecution(store, factory)
        work = asyncio.create_task(runtime.run(request()))
        client = await started(factory, "one")
        with pytest.raises(ValueError):
            await runtime.run(request(prompt="different"))
        client.release.set()
        assert (await work).status == "accepted"
    asyncio.run(run())


def test_snapshots_are_immutable_and_capacity_is_validated():
    snapshot = Snapshot(1, [Message("user", "a")])
    assert isinstance(snapshot.messages, tuple)
    with pytest.raises(AttributeError):
        snapshot.messages[0].content = "changed"
    for value in (0, -1, True, 1.2):
        with pytest.raises(ValueError):
            SessionExecution(TestAuthority(), Factory(), capacity=value)
