"""The template worker process (``python -I -m hagent_app.rules.render_worker``).

The app starts a small pool of these processes (``rules.render_pool``). Each
worker renders one rule template at a time with ``templates.render_sync``.

Terms:

- **Frame**: a 4-byte big-endian length, then that many bytes.
- **Request**: a frame from the app on stdin. It holds a pickle of plain
  data only (see ``render_pool``). The app is the trusted side, so the
  worker can read a pickle.
- **Reply**: a frame from the worker on its protocol pipe. It holds JSON,
  never a pickle, so that a worker that a template took over cannot run
  code in the app.

Limits that the worker sets for itself:

- ``RLIMIT_AS``: the address space after the imports, plus the memory
  budget from the command line.
- ``RLIMIT_CPU``: before each render, the CPU time used so far, plus the
  render time limit, plus a margin. The kernel stops the worker with
  ``SIGXCPU`` if a render passes it.
- ``RLIMIT_CORE``: 0, so that ``SIGXCPU`` writes no core file.

The worker exits when stdin closes, so it never outlives the app.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import pickle
import resource
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO

from hagent_app.rules.templates import (
    MAX_OUTPUT,
    ScopedStates,
    TemplateClock,
    TemplateError,
    TemplateMemoryError,
    render_sync,
    tz_from_key,
)

_HEADER = struct.Struct(">I")
# The largest request: a large scoped view plus the variables.
MAX_REQUEST = 256 * 1024 * 1024
# The largest reply: the output in UTF-8 (4 bytes for each character at
# most, JSON escapes 6) plus the JSON wrapper.
MAX_REPLY = 6 * MAX_OUTPUT + 4096
CPU_MARGIN = 2


def read_frame(stream: BinaryIO, limit: int) -> bytes | None:
    """Read one frame. Return ``None`` at the end of the stream."""
    header = _read_exact(stream, _HEADER.size)
    if header is None:
        return None
    (size,) = _HEADER.unpack(header)
    if size > limit:
        msg = f"a frame of {size} bytes is larger than {limit}"
        raise ValueError(msg)
    body = _read_exact(stream, size)
    if body is None:
        msg = "the stream ended inside a frame"
        raise ValueError(msg)
    return body


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = size
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def frame(body: bytes) -> bytes:
    """A frame that holds ``body``."""
    return _HEADER.pack(len(body)) + body


def _reply(out: BinaryIO, value: dict[str, Any]) -> None:
    out.write(frame(json.dumps(value, ensure_ascii=False).encode()))
    out.flush()


def _vm_size() -> int:
    """The address space of this process in bytes (0 if unknown)."""
    try:
        with Path("/proc/self/status").open(encoding="ascii") as status:
            for line in status:
                if line.startswith("VmSize:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def _limit_memory(budget: int) -> None:
    used = _vm_size()
    if used and budget > 0:
        _soft, hard = resource.getrlimit(resource.RLIMIT_AS)
        limit = used + budget
        if hard != resource.RLIM_INFINITY:
            limit = min(limit, hard)
        resource.setrlimit(resource.RLIMIT_AS, (limit, hard))


def _limit_cpu(seconds: float) -> None:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    used = usage.ru_utime + usage.ru_stime
    _soft, hard = resource.getrlimit(resource.RLIMIT_CPU)
    limit = math.ceil(used + seconds) + CPU_MARGIN
    if hard != resource.RLIM_INFINITY:
        limit = min(limit, hard)
    resource.setrlimit(resource.RLIMIT_CPU, (limit, hard))


def serve(inp: BinaryIO, out: BinaryIO, memory: int) -> None:
    """Answer requests until ``inp`` ends."""
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    _limit_memory(memory)
    _reply(out, {"ready": True})
    view_token = 0
    view = ScopedStates()
    while True:
        body = read_frame(inp, MAX_REQUEST)
        if body is None:
            return
        request = pickle.loads(body)  # noqa: S301 - from the app
        if request["view"] is not None:
            view = ScopedStates.from_snapshot(request["view"])
            view_token = request["view_token"]
        elif request["view_token"] != view_token:
            _reply(out, {"error": "the worker has no state view", "fatal": True})
            return
        seconds = float(request["seconds"])
        _limit_cpu(seconds)
        try:
            text = render_sync(
                view,
                TemplateClock(tz_from_key(request["tz"])),
                seconds,
                request["source"],
                request["variables"],
            )
        except TemplateMemoryError as err:
            # The heap can be in a bad state now. Answer, then stop.
            _reply(out, {"error": str(err), "fatal": True})
            return
        except TemplateError as err:
            _reply(out, {"error": str(err)})
            continue
        _reply(out, {"ok": text})


def main() -> None:
    """The entry point. ``argv[1]`` is the memory budget in bytes."""
    memory = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    # The protocol uses its own copy of stdout. Anything else that writes to
    # stdout goes to stderr.
    out = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    with contextlib.suppress(KeyboardInterrupt, BrokenPipeError):
        serve(sys.stdin.buffer, out, memory)


if __name__ == "__main__":
    main()
