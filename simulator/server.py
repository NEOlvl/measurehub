"""Asyncio TCP server that exposes a :class:`ScpiEngine` on a socket (lines end with ``\\n``)."""

from __future__ import annotations

import asyncio
import threading
from types import TracebackType
from typing import Self

from simulator.scpi import ScpiEngine


class SimulatorServer:
    """Serve one shared simulated instrument to any number of TCP clients."""

    def __init__(self, engine: ScpiEngine, host: str = "127.0.0.1", port: int = 5025) -> None:
        self.engine = engine
        self.host = host
        self.port = port
        self._server: asyncio.Server | None = None

    async def start(self) -> int:
        """Start listening; returns the bound port (useful with ``port=0``)."""
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self.port = self._server.sockets[0].getsockname()[1]
        return self.port

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while raw := await reader.readline():
                line = raw.decode("ascii", errors="replace").strip()
                for reply in self.engine.process_line(line):
                    if reply.drop:
                        return
                    if reply.delay_s > 0:
                        await asyncio.sleep(reply.delay_s)
                    if reply.text is not None:
                        writer.write(reply.text.encode("ascii") + b"\n")
                        await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()


class SimulatorThread:
    """Run a :class:`SimulatorServer` in a background thread (tests, embedded CLI use)."""

    def __init__(self, engine: ScpiEngine, host: str = "127.0.0.1", port: int = 0) -> None:
        self.server = SimulatorServer(engine, host, port)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="simulator", daemon=True)

    @property
    def port(self) -> int:
        return self.server.port

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def start(self) -> int:
        self._thread.start()
        return asyncio.run_coroutine_threadsafe(self.server.start(), self._loop).result(timeout=10)

    def stop(self) -> None:
        if self._thread.is_alive():
            asyncio.run_coroutine_threadsafe(self.server.stop(), self._loop).result(timeout=10)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=10)
        self._loop.close()

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()
