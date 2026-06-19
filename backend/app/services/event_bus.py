from __future__ import annotations

import asyncio
from collections import OrderedDict, defaultdict, deque
from contextlib import suppress
from typing import Any

EVENT_BUFFER_LIMIT = 1000
EVENT_BUFFER_KEY_LIMIT = 200
SUBSCRIBER_QUEUE_LIMIT = 1000


class SessionEventBus:
    def __init__(self) -> None:
        self._buffers: dict[tuple[str, str], deque[tuple[str, dict[str, Any]]]] = defaultdict(
            lambda: deque(maxlen=EVENT_BUFFER_LIMIT)
        )
        self._buffer_key_order: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[tuple[str, dict[str, Any]]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, key: tuple[str, str], event: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._remember_buffer_key(key)
            self._buffers[key].append((event, data))
            for queue in list(self._subscribers.get(key, ())):
                _put_latest(queue, (event, data))

    async def subscribe(
        self, key: tuple[str, str]
    ) -> tuple[list[tuple[str, dict[str, Any]]], asyncio.Queue[tuple[str, dict[str, Any]]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        async with self._lock:
            snapshot = list(self._buffers.get(key, ()))
            self._subscribers[key].add(queue)
        return snapshot, queue

    async def subscribe_live(self, key: tuple[str, str]) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_LIMIT)
        async with self._lock:
            self._subscribers[key].add(queue)
        return queue

    async def unsubscribe(self, key: tuple[str, str], queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
        async with self._lock:
            subscribers = self._subscribers.get(key)
            if subscribers is None:
                return
            subscribers.discard(queue)
            if not subscribers:
                self._subscribers.pop(key, None)

    def _remember_buffer_key(self, key: tuple[str, str]) -> None:
        self._buffer_key_order.pop(key, None)
        self._buffer_key_order[key] = None
        while len(self._buffer_key_order) > EVENT_BUFFER_KEY_LIMIT:
            oldest_key, _ = self._buffer_key_order.popitem(last=False)
            self._buffers.pop(oldest_key, None)


def _put_latest(
    queue: asyncio.Queue[tuple[str, dict[str, Any]]],
    item: tuple[str, dict[str, Any]],
) -> None:
    try:
        queue.put_nowait(item)
        return
    except asyncio.QueueFull:
        with suppress(asyncio.QueueEmpty):
            queue.get_nowait()

    with suppress(asyncio.QueueFull):
        queue.put_nowait(item)
