"""Request-owned model executions, with an injected authoritative session store.

This module owns transient execution handles only. It is not a session database,
message queue, provider implementation, or transport. Production must implement
Authority using its existing admission/lease/delivery authority.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Message:
    role: str
    content: str


@dataclass(frozen=True)
class Route:
    profile: str
    channel: str
    user: str
    thread: str = ""


@dataclass(frozen=True)
class Request:
    session_id: str
    request_id: str
    route: Route
    prompt: str

    def __post_init__(self):
        if not self.session_id or not self.request_id or not all(
                (self.route.profile, self.route.channel, self.route.user)):
            raise ValueError("trusted session, request and route identities are required")


@dataclass(frozen=True)
class Snapshot:
    revision: int
    messages: tuple[Message, ...]

    def __post_init__(self):
        object.__setattr__(self, "messages", tuple(self.messages))
        if any(not isinstance(message, Message) for message in self.messages):
            raise TypeError("snapshot messages must be immutable Message records")


@dataclass(frozen=True)
class Lease:
    request: Request
    generation: str
    snapshot: Snapshot


@dataclass(frozen=True)
class Result:
    status: str  # accepted / duplicate / cancelled / fenced / needs_rebase / failed
    text: str = ""


@dataclass(frozen=True)
class Claim:
    lease: Lease | None = None
    settled: Result | None = None


@dataclass(frozen=True)
class Completion:
    event_id: str
    task_id: str
    owner_session_id: str
    route: Route
    summary: str


class Authority(Protocol):
    async def claim(self, request: Request) -> Claim:
        """Atomically deduplicate payload, authorize route and issue a fenced lease.

        Concurrent ownership must return a settled/pending decision, never two
        active leases. Snapshots belong to the authority, not a cached model.
        """
        ...

    async def commit(self, lease: Lease, text: str) -> Result:
        """Compare lease generation, cancellation, route and snapshot revision.

        Append an anchored result and delivery obligation once. A revision
        conflict must persist needs_rebase, never silently overwrite history.
        No automatic model retry: inference may have included effectful tools.
        """
        ...

    async def fail(self, lease: Lease, reason: str) -> Result: ...
    async def request_cancel(self, request: Request) -> bool: ...
    async def cancelled(self, lease: Lease) -> Result: ...

    async def record_completion(self, completion: Completion) -> Result:
        """Verify persisted task ownership and enqueue its direct delivery once.

        Never call a model, steer a user turn, or infer a target from the current
        foreground request. Uncertain transport outcomes stay in the existing
        delivery ledger; this method does not claim exactly-once network sends.
        """
        ...


class Executor(Protocol):
    async def execute(self, request: Request, snapshot: Snapshot,
                      cancelled: asyncio.Event) -> str: ...
    async def aclose(self) -> None: ...


class ExecutorFactory(Protocol):
    async def open(self, lease: Lease) -> Executor:
        """Create a fresh request-owned client and tool/cancellation context."""
        ...


class SharedExecutorError(RuntimeError):
    pass


@dataclass
class _Flight:
    request: Request
    cancelled: asyncio.Event
    task: asyncio.Task | None = None


class SessionExecution:
    def __init__(self, authority: Authority, factory: ExecutorFactory, *, capacity: int = 4):
        if type(capacity) is not int or capacity < 1:
            raise ValueError("capacity must be a positive integer")
        self.authority, self.factory = authority, factory
        self._capacity = asyncio.Semaphore(capacity)
        self._guard = asyncio.Lock()
        self._flights: dict[tuple[str, str], _Flight] = {}
        self._clients: set[int] = set()

    async def run(self, request: Request) -> Result:
        key = request.session_id, request.request_id
        async with self._guard:
            flight = self._flights.get(key)
            if flight is not None and flight.request != request:
                raise ValueError("request identity reused with a different payload or route")
            if flight is None:
                flight = _Flight(request, asyncio.Event())
                self._flights[key] = flight
                flight.task = asyncio.create_task(self._run(key, flight))
        # A disconnected viewer must not cancel accepted work. Explicit cancel
        # below remains authoritative and is scoped to precisely one request.
        return await asyncio.shield(flight.task)

    async def cancel(self, request: Request) -> bool:
        accepted = await self.authority.request_cancel(request)
        if not accepted:
            return False
        async with self._guard:
            flight = self._flights.get((request.session_id, request.request_id))
            if flight is not None:
                if flight.request != request:
                    raise ValueError("cancellation route/payload mismatch")
                flight.cancelled.set()
                # Do not cancel the orchestration task before it has claimed;
                # the per-request event is passed to its owned executor.
        return True

    async def background_completed(self, completion: Completion) -> Result:
        # No model permit and no per-session execution lock on this path.
        return await self.authority.record_completion(completion)

    async def _run(self, key, flight):
        lease = None
        try:
            claim = await self.authority.claim(flight.request)
            if claim.settled is not None:
                return claim.settled
            lease = claim.lease
            if lease is None or lease.request != flight.request or not lease.generation:
                raise ValueError("authority returned an invalid execution lease")
            async with self._capacity:
                if flight.cancelled.is_set():
                    return await self.authority.cancelled(lease)
                executor = await self.factory.open(lease)
                owned = False
                try:
                    async with self._guard:
                        if id(executor) in self._clients:
                            raise SharedExecutorError("factory reused a live model executor")
                        self._clients.add(id(executor))
                        owned = True
                    # Race model completion against only this request's cancel
                    # signal. Neither task can close another request's client.
                    model = asyncio.create_task(executor.execute(
                        flight.request, lease.snapshot, flight.cancelled))
                    cancellation = asyncio.create_task(flight.cancelled.wait())
                    try:
                        done, _ = await asyncio.wait((model, cancellation),
                                                     return_when=asyncio.FIRST_COMPLETED)
                        if cancellation in done:
                            model.cancel()
                            await asyncio.gather(model, return_exceptions=True)
                            return await self.authority.cancelled(lease)
                        text = await model
                        if not isinstance(text, str):
                            raise TypeError("model output must be text")
                    finally:
                        cancellation.cancel()
                        if not model.done():
                            model.cancel()
                        await asyncio.gather(model, cancellation, return_exceptions=True)
                finally:
                    if owned:
                        try:
                            await executor.aclose()
                        finally:
                            async with self._guard:
                                self._clients.discard(id(executor))
                return await self.authority.commit(lease, text)
        except asyncio.CancelledError:
            if lease is not None:
                await self.authority.cancelled(lease)
            raise
        except Exception as exc:
            if lease is None:
                raise
            # Persist a class code only; provider exception text may contain
            # credentials, request content or URLs with sensitive parameters.
            return await self.authority.fail(lease, type(exc).__name__)
        finally:
            async with self._guard:
                if self._flights.get(key) is flight:
                    del self._flights[key]
