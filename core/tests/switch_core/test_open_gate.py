import asyncio
import json
import logging

import pytest
from starlette.types import Message, Receive, Scope, Send

from switch_core.open_gate import OpenGate


class _Socket:
    """An agent socket: accepted, then a first frame once `opened` is set, then
    a stream that runs until `ended` is set. Counts the opens that reached it."""

    def __init__(self) -> None:
        self.opened = asyncio.Event()
        self.ended = asyncio.Event()
        self.calls = 0

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        self.calls += 1
        await receive()
        await send({"type": "websocket.accept"})
        await self.opened.wait()
        await send({"type": "websocket.send", "text": '{"event": "connection_state"}'})
        await self.ended.wait()
        await send({"type": "websocket.close", "code": 1000})


class _Held:
    """Holds each open until the test lets it answer, and counts the opens that
    reached it: `answers[n]` lets the n-th answer."""

    def __init__(self) -> None:
        self.answers: list[asyncio.Event] = []

    @property
    def calls(self) -> int:
        return len(self.answers)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        answer = asyncio.Event()
        self.answers.append(answer)
        await answer.wait()
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _scope(caller: str, path: str = "/v1/controllers/c1/connection") -> Scope:
    headers = [(b"authorization", f"Bearer {caller}".encode())]
    if path.endswith("/ws"):
        return {"type": "websocket", "path": path, "headers": headers}
    return {"type": "http", "method": "POST", "path": path, "headers": headers}


def _receive(scope: Scope) -> Receive:
    async def receive() -> Message:
        if scope["type"] == "websocket":
            return {"type": "websocket.connect"}
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return receive


async def _open(gate: OpenGate, scope: Scope) -> list[Message]:
    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    await gate(scope, _receive(scope), send)
    return sent


def _told(sent: list[Message]) -> float | None:
    """When a refused open was told to come back, in seconds; None if it opened."""
    if sent[0]["type"] == "websocket.accept":
        assert [m["type"] for m in sent] == [
            "websocket.accept",
            "websocket.send",
            "websocket.close",
        ]
        assert sent[2]["code"] == 4503
        frame = json.loads(sent[1]["text"])
        assert frame["event"] == "refused"
        assert frame["data"]["status"] == 503
        detail = frame["data"]["detail"]
    elif sent[0]["status"] == 503:
        body = json.loads(sent[1]["body"])
        detail = body.get("error") or body["detail"]
    else:
        return None
    assert detail["code"] == "busy"
    return detail["retry_after_s"]


async def _settle() -> None:
    await asyncio.sleep(0.01)


async def _measure(gate: OpenGate, app: _Held, clock: _Clock, seconds: float) -> None:
    """Run one open through the gate that takes `seconds`."""
    task = asyncio.create_task(_open(gate, _scope("measured")))
    await _settle()
    clock.now += seconds
    app.answers[-1].set()
    await task


async def _answer_all(app: _Held, tasks: list[asyncio.Task[list[Message]]]) -> None:
    while not all(task.done() for task in tasks):
        for answer in app.answers:
            answer.set()
        await _settle()


async def test_an_open_past_the_limit_waits_for_a_slot_and_then_runs() -> None:
    app = _Socket()
    gate = OpenGate(app, limit=1, clock=_Clock())
    first = asyncio.create_task(_open(gate, _scope("a", "/agents/a1/connection/ws")))
    await _settle()
    second = asyncio.create_task(_open(gate, _scope("b", "/agents/b1/connection/ws")))
    await _settle()
    assert app.calls == 1

    # The first open's first frame frees its slot, though its stream goes on.
    app.opened.set()
    await _settle()
    assert app.calls == 2

    app.ended.set()
    await asyncio.gather(first, second)


async def test_no_open_is_refused_until_one_has_been_measured() -> None:
    clock = _Clock()
    app = _Held()
    gate = OpenGate(app, limit=2, clock=clock, rand=lambda: 0.0)
    opens = [asyncio.create_task(_open(gate, _scope(f"c{n}"))) for n in range(7)]
    await _settle()
    assert app.calls == 2
    assert not any(task.done() for task in opens)

    clock.now += 1.0  # the first open took 1 s
    app.answers[0].set()
    await _settle()
    # Two of the five waiting keep waiting, and the first of them has the freed
    # slot. The three behind them are told to come back one slot (1 s over 2)
    # apart, after those two.
    assert app.calls == 3
    told = [_told(await task) for task in opens[4:]]
    assert told == pytest.approx([1.5, 2.0, 2.5])

    await _answer_all(app, opens)


async def test_an_open_behind_a_full_line_is_told_when_to_come_back() -> None:
    clock = _Clock()
    app = _Held()
    gate = OpenGate(app, limit=1, clock=clock, rand=lambda: 0.0)
    await _measure(gate, app, clock, 0.5)
    line = [asyncio.create_task(_open(gate, _scope(c))) for c in ("a", "b")]
    await _settle()
    calls = app.calls

    told = _told(await _open(gate, _scope("c", "/agents/c1/connection/ws")))
    # A slot frees every 0.5 s, and "c" is behind "b": two slots from now.
    assert told == pytest.approx(1.0)
    assert app.calls == calls  # refused before authentication or anything else

    await _answer_all(app, line)


async def test_refused_opens_are_given_times_one_slot_apart_in_arrival_order() -> None:
    clock = _Clock()
    gate = OpenGate(_Held(), limit=4, clock=clock, rand=lambda: 0.0)
    gate._open_s.append(0.8)  # a slot frees every 0.8 s over 4
    times = [gate._retry_after(f"caller-{n}", clock.now) for n in range(4)]
    assert times == pytest.approx([0.2, 0.4, 0.6, 0.8])


async def test_the_slots_follow_the_median_of_the_last_opens() -> None:
    clock = _Clock()
    gate = OpenGate(_Held(), limit=3, clock=clock, rand=lambda: 0.0)
    # One open that hung does not move the line.
    gate._open_s.extend([0.3, 30.0, 0.3])
    assert gate._retry_after("a", clock.now) == pytest.approx(0.1)

    # The last three opens took longer, so the slots are further apart.
    clock.now += 10.0
    gate._open_s.extend([0.6, 0.6, 0.6])
    first = gate._retry_after("b", clock.now)
    assert gate._retry_after("c", clock.now) - first == pytest.approx(0.2)


async def test_a_caller_refused_again_before_its_time_keeps_its_time() -> None:
    clock = _Clock()
    gate = OpenGate(_Held(), limit=1, clock=clock, rand=lambda: 0.0)
    gate._open_s.append(1.0)
    for n in range(10):
        gate._retry_after(f"caller-{n}", clock.now)
    first = gate._retry_after("late", clock.now)
    clock.now += 1.0
    # Back too early, as a client that ignores the time is: the same moment,
    # and the line behind it does not move.
    assert gate._retry_after("late", clock.now) == pytest.approx(first - 1.0)
    assert gate._retry_after("next", clock.now) == pytest.approx(first)


async def test_a_caller_back_at_its_time_waits_even_when_the_line_is_full() -> None:
    clock = _Clock()
    app = _Held()
    gate = OpenGate(app, limit=1, clock=clock, rand=lambda: 0.0)
    await _measure(gate, app, clock, 0.5)
    line = [asyncio.create_task(_open(gate, _scope(c))) for c in ("a", "b")]
    await _settle()
    told = _told(await _open(gate, _scope("c")))
    assert told is not None

    clock.now += told
    back = asyncio.create_task(_open(gate, _scope("c")))
    await _settle()
    assert not back.done()  # waiting behind "b", though the line was full
    assert _told(await _open(gate, _scope("d"))) is not None  # a newcomer is not

    # The slots go in arrival order: "a" answers, then "b" runs, then "c".
    app.answers[1].set()
    await _settle()
    assert app.calls == 3
    app.answers[2].set()
    await _settle()
    assert app.calls == 4

    await _answer_all(app, [*line, back])
    assert _told(back.result()) is None


async def test_a_caller_back_long_after_its_time_lines_up_like_anyone_else() -> None:
    clock = _Clock()
    app = _Held()
    gate = OpenGate(app, limit=1, clock=clock, rand=lambda: 0.0)
    await _measure(gate, app, clock, 0.5)
    line = [asyncio.create_task(_open(gate, _scope(c))) for c in ("a", "b")]
    await _settle()
    told = _told(await _open(gate, _scope("c")))
    assert told is not None

    # Back more than one open's time (0.5 s) after its slot: as a time kept
    # from an earlier storm, for a caller that opened on another pod since.
    clock.now += told + 0.6
    again = asyncio.create_task(_open(gate, _scope("c")))
    await _settle()
    assert again.done()  # refused at once, not waiting past the limit
    # A new place, behind "b", not the old one.
    assert _told(again.result()) == pytest.approx(1.0)

    await _answer_all(app, line)


async def test_an_open_that_goes_away_while_waiting_leaves_no_slot_taken() -> None:
    app = _Held()
    gate = OpenGate(app, limit=1, clock=_Clock())
    running = asyncio.create_task(_open(gate, _scope("a")))
    await _settle()
    leaving = asyncio.create_task(_open(gate, _scope("b")))
    staying = asyncio.create_task(_open(gate, _scope("c")))
    await _settle()

    leaving.cancel()
    app.answers[0].set()
    await _settle()
    assert app.calls == 2  # "c" has the slot "b" would have had

    await _answer_all(app, [running, staying])
    # And with every open done, the slot is free for the next.
    nxt = asyncio.create_task(_open(gate, _scope("d")))
    await _settle()
    assert app.calls == 3
    await _answer_all(app, [nxt])


async def test_an_open_handed_a_slot_as_it_goes_away_passes_the_slot_on() -> None:
    app = _Held()
    gate = OpenGate(app, limit=1, clock=_Clock())
    leaving: asyncio.Task[list[Message]] | None = None

    async def send(message: Message) -> None:
        # By its first output "a" has handed its slot to "b", which goes away
        # before it runs.
        if leaving is not None:
            leaving.cancel()

    scope = _scope("a")
    running = asyncio.create_task(gate(scope, _receive(scope), send))
    await _settle()
    leaving = asyncio.create_task(_open(gate, _scope("b")))
    staying = asyncio.create_task(_open(gate, _scope("c")))
    await _settle()

    app.answers[0].set()
    await _settle()
    assert leaving.cancelled()
    assert app.calls == 2  # "c" has the slot

    app.answers[1].set()
    await asyncio.gather(running, staying)


async def test_a_controller_open_is_refused_in_the_management_envelope() -> None:
    clock = _Clock()
    app = _Held()
    gate = OpenGate(app, limit=1, clock=clock)
    await _measure(gate, app, clock, 0.5)
    line = [asyncio.create_task(_open(gate, _scope(c))) for c in ("a", "b")]
    await _settle()

    sent = await _open(gate, _scope("ctl-token"))
    assert sent[0]["status"] == 503
    headers = dict(sent[0]["headers"])
    error = json.loads(sent[1]["body"])["error"]
    assert error["code"] == "busy"
    assert error["retryable"] is True
    assert int(headers[b"retry-after"]) >= error["retry_after_s"]

    await _answer_all(app, line)


async def test_other_routes_and_a_limit_of_0_are_not_paced() -> None:
    app = _Held()
    gate = OpenGate(app, limit=1, clock=_Clock())
    running = asyncio.create_task(_open(gate, _scope("a")))
    await _settle()

    # Every slot taken, yet a message post is not an open and goes straight in.
    post = asyncio.create_task(_open(gate, _scope("a", "/agents/a1/message")))
    await _settle()
    assert app.calls == 2

    off = OpenGate(app, limit=0, clock=_Clock())
    opening = asyncio.create_task(_open(off, _scope("b")))
    await _settle()
    assert app.calls == 3

    await _answer_all(app, [running, post, opening])


async def test_one_line_is_logged_when_refusing_starts_and_one_when_it_is_over(
    caplog: pytest.LogCaptureFixture,
) -> None:
    clock = _Clock()
    app = _Held()
    gate = OpenGate(app, limit=1, clock=clock, rand=lambda: 0.0)
    await _measure(gate, app, clock, 0.5)
    line = [asyncio.create_task(_open(gate, _scope(c))) for c in ("a", "b")]
    await _settle()

    with caplog.at_level(logging.WARNING, logger="switch_core.open_gate"):
        for caller in ("c", "d", "e"):
            assert _told(await _open(gate, _scope(caller))) is not None
        assert len(caplog.records) == 1

        clock.now += 10.0  # every time given has passed
        await _answer_all(app, line)
    assert len(caplog.records) == 2
    assert "3 were told to come back" in caplog.records[1].getMessage()
