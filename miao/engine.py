"""Own the isolated Node process and bounded bidirectional JSON-line requests.

Example:
    async with Engine(runtime_root) as engine:
        status = await engine.request("health", {})

The worker has no QQ login. Platform and account calls return to an explicit
Python callback; a child process cannot request arbitrary Python execution.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path

Callback = Callable[[str, dict], Awaitable[object]]
LOG = logging.getLogger(__name__)
MAX_LINE = 8 * 1024 * 1024
# The bridge routes console output to stderr. Upstream reports failures with
# console.log(err), so these markers decide what is worth keeping on disk.
ERROR_MARKS = (
    "Error:",
    "Error [",
    "TypeError",
    "ReferenceError",
    "RangeError",
    "SyntaxError",
    "UnhandledPromiseRejection",
    "ECONNRESET",
    "ECONNREFUSED",
    "ETIMEDOUT",
    "ENOTFOUND",
    "失败",
)
STACK_LINES = 6


class EngineError(RuntimeError):
    """Report a worker failure without exposing remote stack or credentials."""


class Engine:
    """Keep worker requests isolated and release all resources on shutdown."""

    def __init__(
        self,
        root: Path,
        *,
        node: str = "node",
        timeout: float = 45,
        callback: Callback | None = None,
    ):
        self.root = Path(root).resolve()
        self.node = node
        self.timeout = timeout
        self.callback = callback
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[str, asyncio.Future] = {}
        self.callbacks: set[asyncio.Task] = set()
        self.readers: list[asyncio.Task] = []
        self.write_lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.sequence = 0
        self.closing = False

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def start(self):
        """Start one prepared runtime, never a Yunzai or QQ process.

        Raises:
            EngineError: If the runtime is missing or the engine is closed.
        """
        async with self.start_lock:
            if self.closing:
                raise EngineError("Engine is closed")
            if self.process is not None and self.process.returncode is None:
                return
            if self.readers:
                await asyncio.gather(*self.readers, return_exceptions=True)
                self.readers.clear()
            entry = self.root / "bridge" / "main.mjs"
            if not entry.is_file():
                raise EngineError("Runtime is not prepared")
            self.process = await asyncio.create_subprocess_exec(
                self.node,
                str(entry),
                cwd=self.root,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=MAX_LINE,
            )
            process = self.process
            self.readers = [
                asyncio.create_task(self._read(process)),
                asyncio.create_task(self._logs(process)),
            ]

    async def _write(self, packet: dict):
        raw = (json.dumps(packet, ensure_ascii=False) + "\n").encode()
        if len(raw) > MAX_LINE:
            raise EngineError("Worker packet exceeds limit")
        async with self.write_lock:
            if not self.process or self.process.returncode is not None:
                raise EngineError("Worker is not running")
            self.process.stdin.write(raw)
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict, *, timeout: float | None = None):
        """Run an isolated request and cancel it if the caller or deadline ends.

        Args:
            method: An action explicitly implemented by the worker.
            params: JSON-serializable action arguments.
            timeout: Optional trusted deadline for long-running media recognition.

        Returns:
            The worker's JSON result.

        Raises:
            EngineError: If the worker exits or rejects the request.
            TimeoutError: If the action exceeds its deadline.
        """
        await self.start()
        if len(self.pending) >= 16:
            raise EngineError("Worker request capacity exceeded")
        self.sequence += 1
        ident = str(self.sequence)
        future = asyncio.get_running_loop().create_future()
        self.pending[ident] = future
        try:
            await self._write(
                {"kind": "request", "id": ident, "method": method, "params": params}
            )
            return await asyncio.wait_for(
                future, timeout if timeout is not None else self.timeout
            )
        except (asyncio.CancelledError, TimeoutError):
            try:
                await self._write({"kind": "cancel", "id": ident})
            except (EngineError, BrokenPipeError, ConnectionResetError):
                pass
            raise
        finally:
            self.pending.pop(ident, None)

    async def _read(self, process: asyncio.subprocess.Process):
        """Drain responses while callback jobs run independently to avoid deadlock."""
        try:
            while raw := await process.stdout.readline():
                try:
                    packet = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    raise EngineError(
                        "Worker emitted an invalid protocol packet"
                    ) from None
                if packet.get("kind") == "callback":
                    if len(self.callbacks) >= 32:
                        await self._write(
                            {
                                "kind": "callback_result",
                                "id": packet.get("id"),
                                "error": "Callback capacity exceeded",
                            }
                        )
                        continue
                    task = asyncio.create_task(self._callback(packet))
                    self.callbacks.add(task)
                    task.add_done_callback(self.callbacks.discard)
                    continue
                future = self.pending.get(str(packet.get("id")))
                if future is None or future.done():
                    continue
                if packet.get("error"):
                    future.set_exception(EngineError(str(packet["error"])[:300]))
                else:
                    future.set_result(packet.get("result"))
        except (OSError, ValueError, EngineError) as exc:
            LOG.warning("Miao worker protocol stopped: %s", type(exc).__name__)
        finally:
            for future in tuple(self.pending.values()):
                if not future.done():
                    future.set_exception(EngineError("Worker disconnected"))

    async def _callback(self, packet: dict):
        try:
            if self.callback is None:
                raise EngineError("Callback is unavailable")
            result = await asyncio.wait_for(
                self.callback(
                    str(packet.get("method", "")), packet.get("params") or {}
                ),
                self.timeout,
            )
            response = {"kind": "callback_result", "id": packet["id"], "result": result}
        except Exception:
            # Remote callers get a stable error, not a traceback containing data.
            response = {
                "kind": "callback_result",
                "id": packet["id"],
                "error": "Platform callback failed",
            }
        try:
            await self._write(response)
        except (EngineError, BrokenPipeError, ConnectionResetError):
            pass

    async def _logs(self, process: asyncio.subprocess.Process):
        """Keep worker failure output readable without persisting all chatter.

        The bridge already keeps its own logging off the stdout protocol stream,
        so stderr is the only place an upstream exception or stack can appear.
        Ordinary diagnostics stay at debug level; a marked line promotes itself
        and the following stack frames to warning so a failed command leaves a
        cause behind.
        """
        follow = 0
        while raw := await process.stderr.readline():
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            if any(mark in line for mark in ERROR_MARKS):
                follow = STACK_LINES
                LOG.warning("Miao worker error: %s", line[:400])
            elif follow:
                follow -= 1
                LOG.warning("Miao worker detail: %s", line[:200])
            else:
                LOG.debug("Miao worker diagnostic received")

    async def close(self):
        """Cancel callbacks and stop the child without leaving unread pipes."""
        self.closing = True
        for task in tuple(self.callbacks):
            task.cancel()
        await asyncio.gather(*self.callbacks, return_exceptions=True)
        process = self.process
        if process is not None and process.returncode is None:
            try:
                await self._write(
                    {
                        "kind": "request",
                        "id": "shutdown",
                        "method": "shutdown",
                        "params": {},
                    }
                )
                await asyncio.wait_for(process.wait(), 5)
            except (TimeoutError, EngineError, BrokenPipeError, ConnectionResetError):
                process.kill()
                await process.wait()
        for task in self.readers:
            task.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        self.readers.clear()
        self.process = None
