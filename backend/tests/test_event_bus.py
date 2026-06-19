from __future__ import annotations

import pytest

from app.services.event_bus import EVENT_BUFFER_KEY_LIMIT, SUBSCRIBER_QUEUE_LIMIT, SessionEventBus


@pytest.mark.anyio
async def test_unsubscribe_removes_empty_subscriber_key() -> None:
    bus = SessionEventBus()
    key = ("proj_test", "sess_test")

    queue = await bus.subscribe_live(key)
    assert key in bus._subscribers

    await bus.unsubscribe(key, queue)

    assert key not in bus._subscribers


@pytest.mark.anyio
async def test_subscribe_without_buffer_does_not_create_empty_buffer() -> None:
    bus = SessionEventBus()
    key = ("proj_test", "sess_missing")

    snapshot, queue = await bus.subscribe(key)
    await bus.unsubscribe(key, queue)

    assert snapshot == []
    assert key not in bus._buffers


@pytest.mark.anyio
async def test_publish_without_subscribers_does_not_create_empty_subscriber_key() -> None:
    bus = SessionEventBus()
    key = ("proj_test", "sess_no_subscribers")

    await bus.publish(key, "assistant_delta", {"text": "hello"})

    assert key not in bus._subscribers
    assert list(bus._buffers[key]) == [("assistant_delta", {"text": "hello"})]


@pytest.mark.anyio
async def test_subscriber_queue_is_bounded_and_keeps_latest_events() -> None:
    bus = SessionEventBus()
    key = ("proj_test", "sess_slow_subscriber")
    queue = await bus.subscribe_live(key)
    try:
        assert queue.maxsize == SUBSCRIBER_QUEUE_LIMIT

        for index in range(SUBSCRIBER_QUEUE_LIMIT + 1):
            await bus.publish(key, "assistant_delta", {"index": index})

        assert queue.qsize() == SUBSCRIBER_QUEUE_LIMIT
        first_event, first_payload = await queue.get()
        assert first_event == "assistant_delta"
        assert first_payload == {"index": 1}

        latest_payload = first_payload
        while not queue.empty():
            _event, latest_payload = await queue.get()
        assert latest_payload == {"index": SUBSCRIBER_QUEUE_LIMIT}
    finally:
        await bus.unsubscribe(key, queue)


@pytest.mark.anyio
async def test_event_buffers_evict_oldest_session_keys() -> None:
    bus = SessionEventBus()
    oldest_key = ("proj_test", "sess_000")

    for index in range(EVENT_BUFFER_KEY_LIMIT + 1):
        await bus.publish(("proj_test", f"sess_{index:03d}"), "assistant_delta", {"index": index})

    assert oldest_key not in bus._buffers
    assert len(bus._buffers) == EVENT_BUFFER_KEY_LIMIT
    latest_snapshot, latest_queue = await bus.subscribe(("proj_test", f"sess_{EVENT_BUFFER_KEY_LIMIT:03d}"))
    try:
        assert latest_snapshot == [
            ("assistant_delta", {"index": EVENT_BUFFER_KEY_LIMIT}),
        ]
    finally:
        await bus.unsubscribe(("proj_test", f"sess_{EVENT_BUFFER_KEY_LIMIT:03d}"), latest_queue)
