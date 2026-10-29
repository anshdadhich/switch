"""Paces connection opens, so a reconnect storm becomes a queue.

When a pod goes away, every agent and controller it held opens a connection on
the pods that are left, all within a few seconds. Each open authenticates,
makes a new incarnation of the connection and reads where the stream starts:
dozens of transactions, on the same pool the process's requests and its own
lease renewal use. Measured on staging (two pods, 1,000 agents, one pod
deleted), the opens took the whole pool: thousands of pool timeouts, opens
that failed after their incarnation was made, and the surviving pod's lease
lapsing, which made it drop agents of its own.

This middleware runs at most `limit` opens at once in this process, and takes
every other number it needs from how long opens take here. An open that finds
every slot taken waits for one, in arrival order, if fewer than `limit` opens
are waiting already: those get the next slots to free, each within about the
time one open takes. Any other open is refused before authentication or the
database, with `retry_after_s`, its place in the line. A slot frees every
median open time over `limit` (the median of the last `limit` opens), so a
storm comes back spread over the time the process needs to take it, and the
line slows down when the database does. Until one open has finished there is
nothing to measure, so no open is refused; when the first finishes, the opens
waiting past the first `limit` are told their times.

A caller refused again before its time keeps that time, so a client that
retries at once cannot push everyone else's back, and a caller back at its time
waits for a slot even when `limit` are waiting, so the opens that arrived since
cannot push it back either. At its time means up to one median open time
after it. Later than that, the caller has missed its slot and lines up like
anyone else. That matters beyond the one caller: a caller refused here often
opens on another pod, so this process's times outlive the storm they were
given in, and taken as still due they would let a whole later storm past the
limit on waiting, to wait longer than the proxies in front of this process
allow. A proxy that gives up leaves an open this process still runs, which
moves the connection on behind its client's back.

An open is done when its first frame or response body goes out (the
`connection_state`, or a refusal) or the request ends. What follows is the
stream, which the event buffer's own cap paces. The state is this process's
own: each pod paces its own opens, as each has its own pool.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import math
import random
import re
import statistics
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from switch_core.observability.catalogue import AGENT_OPENS_DEFERRED
from switch_core.observability.metrics import metrics
from switch_core.refusal_breaker import bearer_caller

logger = logging.getLogger(__name__)

# The gate answers before authentication, so a caller that never authenticates
# takes a place in the line too. These bound what such callers can cost: how
# far back they push the times real callers are given, and the memory their
# places take.
MAX_RETRY_S = 300.0
MAX_TRACKED = 10_000

BUSY = "busy"
_STATUS = 503

# The opens it paces, by the label its metric carries.
_ROUTES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^/agents/[^/]+/connection/ws$"), "agent_socket"),
    (re.compile(r"^/agents/[^/]+/events$"), "agent_event_stream"),
    (re.compile(r"^/v1/controllers/[^/]+/connection$"), "controller_open"),
    (re.compile(r"^/v1/controllers/[^/]+/connection/ws$"), "controller_socket"),
)
# Controllers read refusals from the management error envelope; everything
# else on this app reads FastAPI's `detail`.
_MANAGEMENT_PREFIXES = ("/v1/controllers/", "/v1/management/")


def _route(scope: Scope) -> str | None:
    if scope["type"] not in ("http", "websocket"):
        return None
    path = scope.get("path", "")
    if scope["type"] == "http" and scope.get("method") not in ("GET", "POST"):
        return None
    for pattern, label in _ROUTES:
        if pattern.match(path):
            return label
    return None


@dataclass(eq=False)
class _Waiter:
    caller: str | None
    # Set to None when a slot is handed to this open, or to when it is to come
    # back, in seconds.
    told: asyncio.Future[float | None]


class OpenGate:
    def __init__(
        self,
        app: ASGIApp,
        *,
        limit: int,
        clock: Callable[[], float],
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.app = app
        self.limit = limit
        self._clock = clock
        self._rand = rand
        self._slots = max(limit, 1)
        self._running = 0
        self._waiting: deque[_Waiter] = deque()
        # How long the last `limit` opens took, from taking a slot to their
        # first frame or response.
        self._open_s: deque[float] = deque(maxlen=self._slots)
        # The end of the line of callers told when to come back.
        self._next_at = 0.0
        self._given: OrderedDict[str, float] = OrderedDict()
        self._refusing_since: float | None = None
        self._refused = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        route = _route(scope) if self.limit > 0 else None
        if route is None:
            await self.app(scope, receive, send)
            return
        retry_after_s = await self._enter(bearer_caller(scope))
        if retry_after_s is not None:
            metrics().increment(AGENT_OPENS_DEFERRED, {"route": route})
            self._note_refused(retry_after_s)
            await _refuse(scope, receive, send, retry_after_s)
            return

        started = self._clock()
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                self._release(max(self._clock() - started, 0.0))

        async def done_on_first_output(message: Message) -> None:
            if message["type"] in (
                "websocket.send",
                "websocket.close",
                "http.response.body",
            ):
                release()
            await send(message)

        try:
            await self.app(scope, receive, done_on_first_output)
        finally:
            release()

    async def _enter(self, caller: str | None) -> float | None:
        """Take a slot, waiting for one when this open is near enough the front
        of the line. None once the open holds a slot; otherwise when it is to
        come back, in seconds."""
        now = self._clock()
        if self._running < self._slots and not self._waiting:
            self._running += 1
            self._forget(caller)
            return None
        given = self._given.get(caller) if caller is not None else None
        on_time = given is not None and given <= now <= given + self._open_time()
        if given is not None and given + self._open_time() < now:
            self._forget(caller)  # it missed its slot
        if self._open_s and len(self._waiting) >= self._slots and not on_time:
            return self._retry_after(caller, now)

        waiter = _Waiter(caller, asyncio.get_running_loop().create_future())
        self._waiting.append(waiter)
        try:
            told = await waiter.told
        except asyncio.CancelledError:
            if waiter.told.cancelled():
                with contextlib.suppress(ValueError):
                    self._waiting.remove(waiter)
            elif waiter.told.result() is None:
                # Handed a slot as the request went away: the next one has it.
                self._pass_slot()
            raise
        if told is None:
            self._forget(caller)
        return told

    def _release(self, took: float) -> None:
        first = not self._open_s
        self._open_s.append(took)
        if first:
            self._tell_the_line()
        self._pass_slot()
        self._note_line_through()

    def _pass_slot(self) -> None:
        while self._waiting:
            waiter = self._waiting.popleft()
            if not waiter.told.done():
                waiter.told.set_result(None)
                return
        self._running -= 1

    def _tell_the_line(self) -> None:
        """Tell the opens that waited past the first `limit`, while there was
        nothing to measure yet, when to come back."""
        self._waiting = deque(w for w in self._waiting if not w.told.done())
        past = list(itertools.islice(self._waiting, self._slots, None))
        for _ in past:
            self._waiting.pop()
        now = self._clock()
        for waiter in past:
            waiter.told.set_result(self._retry_after(waiter.caller, now))

    def _retry_after(self, caller: str | None, now: float) -> float:
        """When this caller is to come back, in seconds: its place in the line.

        A slot frees every median open time over `limit`. The first caller told
        to come back is behind the opens waiting here, each one after it a slot
        later, at a random point within its slot. A caller already given a time
        that has not come keeps it."""
        if caller is not None:
            at = self._given.get(caller)
            if at is not None and at > now:
                self._given.move_to_end(caller)
                return at - now
        spacing = self._open_time() / self._slots
        start = max(now + (len(self._waiting) + 1) * spacing, self._next_at)
        self._next_at = start + spacing
        at = min(start + spacing * self._rand(), now + MAX_RETRY_S)
        if caller is not None:
            self._given[caller] = at
            self._given.move_to_end(caller)
            while len(self._given) > MAX_TRACKED:
                self._given.popitem(last=False)
        return at - now

    def _open_time(self) -> float:
        """How long an open takes here: the median of the last `limit`."""
        return statistics.median(self._open_s) if self._open_s else 0.0

    def _forget(self, caller: str | None) -> None:
        if caller is not None:
            self._given.pop(caller, None)

    def _note_refused(self, retry_after_s: float) -> None:
        self._refused += 1
        if self._refusing_since is not None:
            return
        self._refusing_since = self._clock()
        logger.warning(
            "Opening connections more slowly than they arrive: %d opening and %d "
            "waiting for a slot, the rest told when to come back, the first in %.1f s",
            self._running,
            len(self._waiting),
            retry_after_s,
        )

    def _note_line_through(self) -> None:
        now = self._clock()
        if self._refusing_since is None or self._waiting or self._next_at > now:
            return
        logger.warning(
            "Caught up with connection opens: %d were told to come back later "
            "over %.0f s",
            self._refused,
            now - self._refusing_since,
        )
        self._refusing_since = None
        self._refused = 0


async def _refuse(
    scope: Scope, receive: Receive, send: Send, retry_after_s: float
) -> None:
    """Refuse the open in the form its client reads refusals in, with when to
    come back."""
    # Rounded up to the millisecond clients wait in, so none comes back early.
    seconds = math.ceil(retry_after_s * 1000) / 1000
    message = (
        "This server is opening as many connections as it can take right now. "
        f"Open again in {seconds:g} s."
    )
    path = scope.get("path", "")
    if scope["type"] == "websocket":
        # As every refused agent or controller socket: accepted, a `refused`
        # frame with the status and detail, then closed with 4000 plus it.
        await receive()  # the client's websocket.connect
        await send({"type": "websocket.accept"})
        frame = {
            "event": "refused",
            "data": {
                "status": _STATUS,
                "detail": {
                    "code": BUSY,
                    "message": message,
                    "retry_after_s": seconds,
                },
            },
        }
        await send({"type": "websocket.send", "text": json.dumps(frame)})
        await send({"type": "websocket.close", "code": 4000 + _STATUS})
        return
    if path.startswith(_MANAGEMENT_PREFIXES):
        body: dict[str, object] = {
            "error": {
                "code": BUSY,
                "message": message,
                "retryable": True,
                "retry_after_s": seconds,
            }
        }
    else:
        body = {"detail": {"code": BUSY, "message": message, "retry_after_s": seconds}}
    payload = json.dumps(body).encode()
    await send(
        {
            "type": "http.response.start",
            "status": _STATUS,
            "headers": [
                (b"content-type", b"application/json"),
                (b"retry-after", str(math.ceil(retry_after_s)).encode()),
                (b"content-length", str(len(payload)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": payload})
