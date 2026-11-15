"""The pool of template worker processes (design section 10.2).

A rule template is text from a model. The app renders it in a separate
worker process (``rules.render_worker``), so that a template cannot hold
the app or its memory:

- The worker sets its own memory and CPU limits.
- The app kills and replaces a worker that does not answer inside the
  render time limit plus ``GRACE_SECONDS``. This also stops one slow call
  in C, for example a regular expression that backtracks.
- The worker starts with an empty environment (``TZ`` only), so that it
  does not get the HA token. Its working directory is ``/``.
- The app sends only plain data: the scoped state snapshot, the time zone
  and the variables. The worker answers with JSON.

Terms:

- **Worker**: one template worker process.
- **Pool**: up to ``RENDER_WORKERS`` workers. Each render takes an idle
  worker or starts a new one. A worker that failed is not used again.
- **Owner**: the agent of a render. One owner can use at most
  ``per_owner`` workers at a time, so that one agent with parallel
  invocations cannot hold all the workers.

The pool does its blocking work in its own threads. It does not depend on
one event loop, so the app can share one pool.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import json
import logging
import os
import pickle
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from select import POLLIN
from select import poll as select_poll
from typing import Any

from hagent_app.rules.render_worker import MAX_REPLY, frame
from hagent_app.rules.templates import (
    RENDER_WORKERS,
    ScopedStates,
    TemplateClock,
    TemplateError,
    plain_data,
    tz_key,
)

_LOG = logging.getLogger(__name__)

# The length of a frame header (see render_worker).
_HEADER = 4
# The extra wait after the render time limit before the app kills a worker.
GRACE_SECONDS = 0.5
# The memory that a worker can use for renders, on top of its imports.
WORKER_MEMORY = 256 * 1024 * 1024
# The time that a new worker can take to start.
START_SECONDS = 30.0
# The longest wait for a free worker.
QUEUE_SECONDS = 10.0


class _Worker:
    """One worker process, used by one thread at a time."""

    def __init__(self, memory: int, start_seconds: float) -> None:
        env = {"TZ": os.environ["TZ"]} if "TZ" in os.environ else {}
        self.proc = subprocess.Popen(  # noqa: S603 - a fixed command
            [
                sys.executable,
                "-I",
                "-m",
                "hagent_app.rules.render_worker",
                str(memory),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd="/",
            close_fds=True,
            bufsize=0,
        )
        self.view_token = 0
        self._buffer = b""
        try:
            ready = self.read(time.monotonic() + start_seconds)
        except TemplateError:
            self.kill()
            raise
        if ready != {"ready": True}:
            self.kill()
            msg = "the template worker did not start"
            raise TemplateError(msg)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, body: bytes) -> None:
        stdin = self.proc.stdin
        assert stdin is not None  # noqa: S101 - Popen sets it
        data = memoryview(frame(body))
        while data:
            written = os.write(stdin.fileno(), data)
            data = data[written:]

    def read(self, deadline: float) -> dict[str, Any] | None:
        """Read one reply. Return ``None`` if the deadline passes."""
        stdout = self.proc.stdout
        assert stdout is not None  # noqa: S101 - Popen sets it
        fd = stdout.fileno()
        while True:
            if len(self._buffer) >= _HEADER:
                size = int.from_bytes(self._buffer[:_HEADER], "big")
                if size > MAX_REPLY:
                    msg = "the template worker sent a reply that is too large"
                    raise TemplateError(msg)
                end = _HEADER + size
                if len(self._buffer) >= end:
                    body = self._buffer[_HEADER:end]
                    self._buffer = self._buffer[end:]
                    reply = json.loads(body)
                    if not isinstance(reply, dict):
                        msg = "the template worker sent a bad reply"
                        raise TemplateError(msg)
                    return reply
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            # poll, not select: select fails for a descriptor above 1023.
            poller = select_poll()
            poller.register(fd, POLLIN)
            if not poller.poll(remaining * 1000):
                return None
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                msg = "the template worker stopped"
                raise TemplateError(msg)
            self._buffer += chunk

    def kill(self) -> None:
        with contextlib.suppress(OSError):
            self.proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=5)
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def close(self) -> None:
        """Close stdin, so that the worker exits. Kill it if it does not."""
        if self.proc.stdin is not None:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(timeout=1)
        self.kill()


class RenderPool:
    """Renders rule templates in worker processes with hard limits."""

    def __init__(  # noqa: PLR0913 - keyword-only limits
        self,
        size: int = RENDER_WORKERS,
        *,
        memory: int = WORKER_MEMORY,
        grace: float = GRACE_SECONDS,
        start_seconds: float = START_SECONDS,
        queue_seconds: float = QUEUE_SECONDS,
        per_owner: int | None = None,
    ) -> None:
        self.size = size
        self.per_owner = per_owner or max(1, size // 2)
        self._owners: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Semaphore]]
        self._owners = {}
        self._memory = memory
        self._grace = grace
        self._start_seconds = start_seconds
        self._queue_seconds = queue_seconds
        self._threads = ThreadPoolExecutor(
            max_workers=size, thread_name_prefix="rule-template"
        )
        self._lock = threading.Lock()
        self._idle: list[_Worker] = []
        self._all: set[_Worker] = set()
        self.started = 0
        self.killed = 0

    async def render(  # noqa: PLR0913 - owner is keyword-only
        self,
        view: ScopedStates,
        clock: TemplateClock,
        seconds: float,
        source: str,
        variables: Mapping[str, Any],
        *,
        owner: str = "",
    ) -> str:
        """Render one template in a worker. Raise ``TemplateError``."""
        async with self._owner_slot(owner):
            return await self._render(view, clock, seconds, source, variables)

    def _owner_slot(self, owner: str) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        with self._lock:
            entry = self._owners.get(owner)
            if entry is None or entry[0] is not loop:
                entry = (loop, asyncio.Semaphore(self.per_owner))
                self._owners[owner] = entry
            return entry[1]

    async def _render(
        self,
        view: ScopedStates,
        clock: TemplateClock,
        seconds: float,
        source: str,
        variables: Mapping[str, Any],
    ) -> str:
        request = {
            "view_token": view.token,
            "tz": tz_key(clock.tz),
            "seconds": seconds,
            "source": source,
            "variables": plain_data(dict(variables)),
        }
        loop = asyncio.get_running_loop()
        queued = time.monotonic()
        future = loop.run_in_executor(
            self._threads, self._render_blocking, view, request, queued
        )
        # The thread always ends: it kills a worker that does not answer.
        # This outer wait is a last resort only.
        limit = self._queue_seconds + self._start_seconds + seconds + self._grace + 5
        try:
            return await asyncio.wait_for(future, limit)
        except TimeoutError:
            msg = f"the template took longer than {seconds} s"
            raise TemplateError(msg) from None

    def _render_blocking(
        self, view: ScopedStates, request: dict[str, Any], queued: float
    ) -> str:
        if time.monotonic() - queued > self._queue_seconds:
            msg = "all template workers are busy"
            raise TemplateError(msg)
        worker = self._acquire()
        healthy = False
        try:
            reply = self._call(worker, view, request)
            healthy = worker.alive and not reply.get("fatal")
        finally:
            self._release(worker, healthy=healthy)
        if "ok" in reply and isinstance(reply["ok"], str):
            return reply["ok"]
        raise TemplateError(str(reply.get("error", "the template worker failed")))

    def _call(
        self, worker: _Worker, view: ScopedStates, request: dict[str, Any]
    ) -> dict[str, Any]:
        seconds = float(request["seconds"])
        if worker.view_token != view.token:
            request = {**request, "view": view.snapshot()}
        else:
            request = {**request, "view": None}
        try:
            worker.send(pickle.dumps(request))
        except OSError:
            msg = "the template worker stopped"
            raise TemplateError(msg) from None
        worker.view_token = view.token
        reply = worker.read(time.monotonic() + seconds + self._grace)
        if reply is None:
            _LOG.info("killed a template worker after %s s", seconds)
            msg = f"the template took longer than {seconds} s"
            raise TemplateError(msg)
        return reply

    def _acquire(self) -> _Worker:
        with self._lock:
            while self._idle:
                worker = self._idle.pop()
                if worker.alive:
                    return worker
                self._discard(worker)
        worker = _Worker(self._memory, self._start_seconds)
        with self._lock:
            self._all.add(worker)
            self.started += 1
        return worker

    def _release(self, worker: _Worker, *, healthy: bool) -> None:
        if healthy:
            with self._lock:
                self._idle.append(worker)
            return
        worker.kill()
        with self._lock:
            self._discard(worker)
            self.killed += 1

    def _discard(self, worker: _Worker) -> None:
        self._all.discard(worker)

    def close(self) -> None:
        """Stop every worker."""
        with self._lock:
            workers = list(self._all)
            self._all.clear()
            self._idle.clear()
        for worker in workers:
            worker.close()
        self._threads.shutdown(wait=False, cancel_futures=True)


_SHARED: RenderPool | None = None
_SHARED_LOCK = threading.Lock()


def shared_pool() -> RenderPool:
    """The render pool of the app process. It starts workers on first use."""
    global _SHARED  # noqa: PLW0603 - one pool for the process
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = RenderPool()
            atexit.register(_SHARED.close)
        return _SHARED
