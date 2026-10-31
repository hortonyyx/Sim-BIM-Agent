"""Event-loop shared route admission; no model payloads or domain knowledge."""

from __future__ import annotations

import asyncio
import json
import math
import time
import weakref
from collections import deque
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class DispatchPolicy:
    initial_concurrency: int
    max_concurrency: int
    recovery_seconds: float = 60.0
    cooldown_seconds: float = 1.0
    max_cooldown_seconds: float = 30.0

    def __post_init__(self):
        if (type(self.initial_concurrency) is not int or type(self.max_concurrency) is not int
                or not 1 <= self.initial_concurrency <= self.max_concurrency):
            raise ValueError("dispatch concurrency must be positive ordered integers")
        for value in (self.recovery_seconds, self.cooldown_seconds, self.max_cooldown_seconds):
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError("dispatch intervals must be finite and positive")
        if self.max_cooldown_seconds < self.cooldown_seconds:
            raise ValueError("maximum cooldown must cover the initial cooldown")


@lru_cache(maxsize=1)
def route_policies():
    config = json.loads(Path(__file__).with_name("route_dispatch.json").read_bytes())
    return {route: DispatchPolicy(**values) for route, values in config["routes"].items()}


class DispatchLease:
    def __init__(self, gate, evidence):
        self.gate, self.evidence = gate, evidence
        self.released = False

    def release(self):
        if not self.released:
            self.released = True
            self.gate.in_flight -= 1
            self.gate._wake()

    def rejected(self):
        return self.gate.rejected()

    def succeeded(self):
        return self.gate.succeeded()


class RouteDispatcher:
    """FIFO requests, with refused attempts readmitted ahead of new requests.

    All state transitions are synchronous on one event loop. A permit is held
    only for a request attempt, never while running tools or awaiting children.
    Persistent refusals are bounded by the caller's deadline/call protection
    limits, with a shared capped cooldown even at concurrency one.
    """

    def __init__(self, route_id, policy, *, clock=time.monotonic):
        self.route_id, self.policy, self.clock = route_id, policy, clock
        self.limit = policy.initial_concurrency
        self.in_flight = 0
        self.queue = deque()
        self.waiters = set()
        self.last_change = clock()
        self.blocked_until = 0.0
        self.refusals = 0

    def _wake(self):
        for future in self.waiters:
            if not future.done():
                future.set_result(None)

    async def acquire(self, *, timeout, redispatch_of=None):
        started, epoch = self.clock(), time.time()
        ticket = object()
        (self.queue.appendleft if redispatch_of else self.queue.append)(ticket)
        try:
            async with asyncio.timeout(timeout):
                while True:
                    now = self.clock()
                    if (self.queue[0] is ticket and self.in_flight < self.limit
                            and now >= self.blocked_until):
                        self.queue.popleft()
                        self.in_flight += 1
                        self._wake()
                        return DispatchLease(self, {
                            "route_id": self.route_id, "policy": asdict(self.policy),
                            "started_epoch": epoch, "ended_epoch": time.time(),
                            "queue_seconds": max(0.0, now - started),
                            "limit": self.limit, "in_flight": self.in_flight,
                            "redispatch_of": redispatch_of,
                        })
                    future = asyncio.get_running_loop().create_future()
                    self.waiters.add(future)
                    try:
                        # Only the head needs a timer; releases wake the rest.
                        delay = self.blocked_until - now
                        if self.queue[0] is ticket and delay > 0:
                            try:
                                await asyncio.wait_for(future, delay)
                            except TimeoutError:
                                pass
                        else:
                            await future
                    finally:
                        self.waiters.discard(future)
        finally:
            if ticket in self.queue:
                self.queue.remove(ticket)
            self._wake()

    def rejected(self):
        previous = self.limit
        self.limit = max(1, self.limit - 1)
        self.last_change = self.clock()
        delay = min(self.policy.max_cooldown_seconds,
                    self.policy.cooldown_seconds * 2 ** min(self.refusals, 16))
        self.refusals += 1
        self.blocked_until = max(self.blocked_until, self.last_change + delay)
        self._wake()
        return {"route_id": self.route_id, "action": "reduce", "previous_limit": previous,
                "limit": self.limit, "cooldown_seconds": delay}

    def succeeded(self):
        now = self.clock()
        if now < max(self.blocked_until, self.last_change + self.policy.recovery_seconds):
            return None
        self.refusals = 0
        if self.limit >= self.policy.max_concurrency:
            return None
        previous = self.limit
        self.limit += 1
        self.last_change = now
        self._wake()
        return {"route_id": self.route_id, "action": "increase", "previous_limit": previous,
                "limit": self.limit}


# Adapters and root journals may differ; concurrent runtimes using the same
# route share admission. Separate event loops/processes have separate queues.
# Idle dispatchers retain no loop/future references, so closed loops can expire.
_dispatchers = weakref.WeakKeyDictionary()


def get_dispatcher(route_id):
    policy = route_policies().get(route_id)
    if policy is None:
        return None
    loop = asyncio.get_running_loop()
    routes = _dispatchers.setdefault(loop, {})
    if route_id not in routes:
        routes[route_id] = RouteDispatcher(route_id, policy)
    elif routes[route_id].policy != policy:
        raise ValueError("route dispatch configuration changed during an active event loop")
    return routes[route_id]
