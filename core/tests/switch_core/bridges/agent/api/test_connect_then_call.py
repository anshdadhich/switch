"""`connect_to_room`, then the next call, through the HTTP front door.

What an agent does first in every room, and what failed when an agent's next
room-scoped call after a successful connect was told it was "not connected".
Each test connects through `POST /agents/{id}/ops/connect_to_room` and makes
the next call the way the agent's runtime does, naming the same caller: a
connection of its own, a session on a connection its siblings share, or an
agent controller acting for the agent. Against Postgres, through the real
middleware, as `test_act_as.py` does.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from switch_core.bridges.agent.operations import context as op_context
from switch_core.bridges.agent.protocol.agent_connections import (
    PROTOCOL_VERSION,
    ClientDeclaration,
)
from switch_core.bridges.agent.protocol.positions import OWN_EVENTS
from switch_core.db.models import TENANT_ZERO_ID, Agent
from switch_core.db.stores.message_store import MessageStore
from switch_core.tenant_context import tenant_scope
from tests.switch_core.management.harness import (
    Harness,
    add_member,
    add_room,
    bearer,
    build_harness,
    enroll_console,
    place_agent,
)
from tests.switch_core.management.test_act_as import _agent_with_key

CONNECTION = "connection-1"


async def _connecting_agent(harness: Harness, name: str) -> tuple[str, str]:
    """An agent with its own API key and a whole integration profile, as one
    that registered has. Returns its id and the key."""
    owner = await add_member(harness.session_factory, f"{name}-owner")
    agent_id, key = await _agent_with_key(harness, owner, name)
    async with harness.session_factory() as session:
        await session.execute(
            update(Agent)
            .where(Agent.id == agent_id)
            .values(
                integration_profile={
                    "connection_model": "auto_session",
                    "message_exchange": True,
                    "pre_invocation_mediation": [],
                    "post_invocation_mediation": [],
                    "event_reporting": [],
                }
            )
        )
        await session.commit()
    return agent_id, key


@pytest.fixture
def harness(session_factory: async_sessionmaker[AsyncSession]) -> Iterator[Harness]:
    harness = build_harness(session_factory)
    protocol: Any = harness.protocol

    async def _nobody(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    async def _no_resources(_room_id: str) -> dict[str, Any]:
        return {
            "linked_rooms": [],
            "reference_types": [],
            "references": [],
            "documents": [],
            "packages": [],
        }

    # What connect_to_room returns about the room besides the binding; not
    # what these tests are about.
    protocol.list_participants = _nobody
    protocol.list_room_roles = _nobody
    protocol.list_room_resources = _no_resources
    # read_context reads the room's stored history: empty here, and real.
    protocol.message_store = MessageStore()
    previous = op_context._protocol
    op_context.init_operations_protocol(protocol)
    try:
        yield harness
    finally:
        op_context._protocol = previous


def _open_connection(harness: Harness, agent_id: str, scope: str) -> None:
    with tenant_scope(TENANT_ZERO_ID):
        harness.protocol.connections.open(
            agent_id=agent_id,
            connection_id=CONNECTION,
            scope=scope,  # type: ignore[arg-type]
            delivery_filter="addressed",
            spawn_capable=scope == "all",
            position={OWN_EVENTS: 0},
            declaration=ClientDeclaration(speaks=PROTOCOL_VERSION),
            expected_generation=None,
        )


def _session(session_id: str) -> dict[str, str]:
    return {
        "X-Switch-Connection-Id": CONNECTION,
        "X-Switch-Session-Id": session_id,
        "X-Switch-Session-Host-Id": "host-1",
        "X-Switch-Session-Epoch": "epoch-1",
    }


async def _op(
    client: httpx.AsyncClient,
    agent_id: str,
    operation: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> httpx.Response:
    return await client.post(
        f"/agents/{agent_id}/ops/{operation}", json=body or {}, headers=headers
    )


async def _read_context_room(
    client: httpx.AsyncClient, agent_id: str, headers: dict[str, str]
) -> httpx.Response:
    """`read_context` with no room named: it resolves the caller's binding,
    which is the step that failed."""
    return await _op(client, agent_id, "read_context", headers, {"limit": 1})


class TestAConnectionOfItsOwn:
    async def test_the_next_call_is_made_from_the_room_it_connected_to(
        self, harness: Harness
    ) -> None:
        agent_id, key = await _connecting_agent(harness, "dedicated")
        room_id = await add_room(harness.session_factory, agent_id)
        _open_connection(harness, agent_id, "single")
        headers = {**bearer(key), "X-Switch-Connection-Id": CONNECTION}

        async with harness.client() as client:
            connected = await _op(
                client, agent_id, "connect_to_room", headers, {"room_id": room_id}
            )
            read = await _read_context_room(client, agent_id, headers)

        assert connected.status_code == 200, connected.text
        assert connected.json()["result"]["room_id"] == room_id
        assert read.status_code == 200, read.text
        assert "threads" in read.json()["result"]


class TestASessionOnASharedConnection:
    async def test_each_sessions_next_call_is_made_from_its_own_room(
        self, harness: Harness
    ) -> None:
        agent_id, key = await _connecting_agent(harness, "shared")
        room_a = await add_room(harness.session_factory, agent_id, name="a")
        room_b = await add_room(harness.session_factory, agent_id, name="b")
        _open_connection(harness, agent_id, "all")

        async with harness.client() as client:
            first = await _op(
                client,
                agent_id,
                "connect_to_room",
                {**bearer(key), **_session("session-a")},
                {"room_id": room_a},
            )
            second = await _op(
                client,
                agent_id,
                "connect_to_room",
                {**bearer(key), **_session("session-b")},
                {"room_id": room_b},
            )
            read_a = await _read_context_room(
                client, agent_id, {**bearer(key), **_session("session-a")}
            )
            read_b = await _read_context_room(
                client, agent_id, {**bearer(key), **_session("session-b")}
            )

        assert first.status_code == 200, first.text
        assert second.status_code == 200, second.text
        assert read_a.status_code == 200, read_a.text
        assert read_b.status_code == 200, read_b.text
        placements = harness.protocol.connections.placements(agent_id)
        assert placements == {"session-a": room_a, "session-b": room_b}


class TestAnAgentController:
    async def test_the_next_call_is_made_from_the_room_the_controller_names(
        self, harness: Harness
    ) -> None:
        owner = await add_member(harness.session_factory, "ada")
        async with harness.client() as client:
            controller = await enroll_console(harness, client, owner)
            agent_id = await place_agent(client, controller, name="reviewer")
            room_id = await add_room(harness.session_factory, agent_id)
            connected = await _op(
                client,
                agent_id,
                "connect_to_room",
                controller.headers,
                {"room_id": room_id},
            )
            read = await _read_context_room(
                client,
                agent_id,
                {**controller.headers, "X-Switch-Room-Id": room_id},
            )

        assert connected.status_code == 200, connected.text
        assert read.status_code == 200, read.text

    async def test_a_call_naming_no_room_says_the_controller_named_none(
        self, harness: Harness
    ) -> None:
        """Switch keeps no record of a controller's sessions: connecting only
        checks membership, and the controller names the room on each call. A
        call it names none for is a controller that did not record the move,
        and is reported as that, not as a session that never connected."""
        owner = await add_member(harness.session_factory, "ada")
        async with harness.client() as client:
            controller = await enroll_console(harness, client, owner)
            agent_id = await place_agent(client, controller, name="reviewer")
            room_id = await add_room(harness.session_factory, agent_id)
            connected = await _op(
                client,
                agent_id,
                "connect_to_room",
                controller.headers,
                {"room_id": room_id},
            )
            read = await _read_context_room(client, agent_id, controller.headers)

        assert connected.status_code == 200, connected.text
        assert read.status_code == 400
        detail = read.json()["detail"]
        assert detail.startswith("Not connected to a room")
        assert "agent controller running this agent named no room" in detail
