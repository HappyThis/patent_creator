from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import Any


class SessionEventBus:
    def __init__(self) -> None:
        self._buffers: dict[tuple[str, str], list[tuple[str, dict[str, Any]]]] = defaultdict(list)
        self._subscribers: dict[tuple[str, str], set[asyncio.Queue[tuple[str, dict[str, Any]]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def publish(self, key: tuple[str, str], event: str, data: dict[str, Any]) -> None:
        async with self._lock:
            self._buffers[key].append((event, data))
            subscribers = list(self._subscribers[key])
        for queue in subscribers:
            queue.put_nowait((event, data))

    async def subscribe(
        self, key: tuple[str, str]
    ) -> tuple[list[tuple[str, dict[str, Any]]], asyncio.Queue[tuple[str, dict[str, Any]]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        async with self._lock:
            snapshot = list(self._buffers[key])
            self._subscribers[key].add(queue)
        return snapshot, queue

    async def subscribe_live(self, key: tuple[str, str]) -> asyncio.Queue[tuple[str, dict[str, Any]]]:
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        async with self._lock:
            self._subscribers[key].add(queue)
        return queue

    async def unsubscribe(self, key: tuple[str, str], queue: asyncio.Queue[tuple[str, dict[str, Any]]]) -> None:
        async with self._lock:
            self._subscribers[key].discard(queue)

    def has_history(self, key: tuple[str, str]) -> bool:
        return key in self._buffers
