"""Contracts for plugin-owned, gateway-rendered human decisions."""

import asyncio
from unittest.mock import patch

import pytest

from hermes_cli.human_decisions import HumanDecisions


def _create(store, *, plugin_id="plugin", timeout_s=30):
    request = store.create(
        plugin_id=plugin_id,
        owner_id=f"owner:{plugin_id}",
        gateway_id="gateway-1",
        title="Choose",
        body="Pick one",
        choices=("approve", "deny"),
        session_key="agent:main:telegram:dm:42",
        session_id="session-1",
        actor_id="42",
        chat_id="42",
        thread_id=None,
        timeout_s=timeout_s,
    )
    assert not isinstance(request, dict)
    return request


def test_resolve_consumes_ticket_once_and_binds_actor_and_chat():
    store = HumanDecisions()
    request = _create(store)

    args = (None, "session-1", "gateway-1")
    assert store.resolve(request.token, 0, "other", "42", *args)["error"] == "unauthorized_actor"
    assert store.resolve(request.token, 0, "42", "other", *args)["error"] == "stale"

    result = store.resolve(request.token, 0, "42", "42", *args)
    assert result == {
        "ok": True,
        "decision": "approve",
        "request_id": request.request_id,
        "actor_id": "42",
    }
    assert store.resolve(request.token, 1, "42", "42", *args)["error"] == "stale"


def test_resolve_binds_thread_session_and_gateway():
    store = HumanDecisions()
    request = store.create(
        plugin_id="plugin",
        owner_id="owner:plugin",
        gateway_id="gateway-1",
        title="Choose",
        body="Pick one",
        choices=("approve", "deny"),
        session_key="session-key",
        session_id="session-1",
        actor_id="42",
        chat_id="chat",
        thread_id="topic-7",
        timeout_s=30,
    )
    assert not isinstance(request, dict)

    assert store.resolve(
        request.token, 0, "42", "chat", "topic-8", "session-1", "gateway-1",
    )["error"] == "stale"
    assert store.resolve(
        request.token, 0, "42", "chat", "topic-7", "session-2", "gateway-1",
    )["error"] == "stale_session"
    assert store.resolve(
        request.token, 0, "42", "chat", "topic-7", "session-1", "gateway-2",
    )["error"] == "stale_session"
    assert request.request_id in store._by_id


@pytest.mark.asyncio
async def test_wait_receives_decision_and_plugin_cancel_wakes_waiter():
    store = HumanDecisions()
    request = _create(store)
    waiting = asyncio.create_task(store.wait(request.request_id))

    assert store.resolve(
        request.token, 1, "42", "42", None, "session-1", "gateway-1",
    )["ok"] is True
    assert (await waiting)["decision"] == "deny"

    cancelled = _create(store, plugin_id="unloaded")
    waiting = asyncio.create_task(store.wait(cancelled.request_id))
    store.cancel_owner("owner:unloaded")
    assert (await waiting)["error"] == "plugin_unloaded"


def test_discard_releases_completed_handoff():
    store = HumanDecisions()
    request = _create(store)

    store.cancel(request.request_id)
    assert request.request_id in store._completed
    store.discard(request.request_id)
    assert request.request_id not in store._completed


@pytest.mark.asyncio
async def test_waiter_cancellation_and_gateway_shutdown_remove_ticket():
    store = HumanDecisions()
    request = _create(store)
    waiting = asyncio.create_task(store.wait(request.request_id))
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    assert request.request_id not in store._by_id
    assert not store._completed

    request = _create(store)
    waiting = asyncio.create_task(store.wait(request.request_id))
    store.cancel_gateway("gateway-1")
    assert (await waiting)["error"] == "gateway_unavailable"


@pytest.mark.asyncio
async def test_owner_cancel_does_not_cross_profile_plugin_scope():
    store = HumanDecisions()
    first = _create(store)
    second = store.create(
        plugin_id="plugin",
        owner_id="other-profile\0plugin",
        gateway_id="gateway-2",
        title="Choose",
        body="Pick one",
        choices=("approve", "deny"),
        session_key="agent:other:telegram:dm:42",
        session_id="session-2",
        actor_id="42",
        chat_id="42",
        thread_id=None,
        timeout_s=30,
    )
    assert not isinstance(second, dict)
    first_wait = asyncio.create_task(store.wait(first.request_id))
    second_wait = asyncio.create_task(store.wait(second.request_id))

    store.cancel_owner("owner:plugin")

    assert (await first_wait)["error"] == "plugin_unloaded"
    assert second.request_id in store._by_id
    assert store.resolve(
        second.token, 0, "42", "42", None, "session-2", "gateway-2",
    )["ok"] is True
    assert (await second_wait)["decision"] == "approve"


@pytest.mark.asyncio
async def test_wait_times_out_and_invalid_requests_fail_closed():
    store = HumanDecisions()
    with patch("hermes_cli.human_decisions.time.monotonic", side_effect=[0, 2]):
        request = _create(store, timeout_s=1)
        assert (await store.wait(request.request_id))["error"] == "timeout"
    assert not store._completed

    invalid = store.create(
        plugin_id="plugin",
        owner_id="owner:plugin",
        gateway_id="gateway-1",
        title="",
        body="body",
        choices=("yes", "no"),
        session_key="s",
        session_id="id",
        actor_id="actor",
        chat_id="chat",
        thread_id=None,
        timeout_s=30,
    )
    assert invalid["error"] == "invalid_argument"
