from __future__ import annotations

import asyncio
import collections
import logging


class LogBuffer(logging.Handler):
    """Thread-safe logging handler that keeps recent records in a ring buffer
    and notifies WebSocket subscribers via asyncio queues.

    ``emit`` runs on whichever thread logged the record, so it schedules the
    put onto the subscriber's event loop rather than blocking on it."""

    def __init__(self, maxlen: int = 2000):
        super().__init__()
        self.buffer: collections.deque[dict] = collections.deque(maxlen=maxlen)
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entry = {
                'level': record.levelname,
                'message': self.format(record),
                'timestamp': record.created,
            }
        except Exception:
            return
        self.buffer.append(entry)
        for loop, q in list(self._subscribers):
            try:
                loop.call_soon_threadsafe(self._offer, q, entry)
            except RuntimeError:
                # Loop already closed; the websocket handler will unsubscribe.
                self.unsubscribe(loop, q)

    @staticmethod
    def _offer(q: asyncio.Queue, entry: dict) -> None:
        try:
            q.put_nowait(entry)
        except asyncio.QueueFull:
            # Slow consumer: drop the oldest entry so the newest still lands.
            try:
                q.get_nowait()
                q.put_nowait(entry)
            except (asyncio.QueueEmpty, asyncio.QueueFull):
                pass

    def recent(self, n: int = 200) -> list[dict]:
        items = list(self.buffer)
        return items[-n:]

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue]:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._subscribers.add((loop, q))
        return loop, q

    def unsubscribe(self, loop: asyncio.AbstractEventLoop, q: asyncio.Queue) -> None:
        self._subscribers.discard((loop, q))
