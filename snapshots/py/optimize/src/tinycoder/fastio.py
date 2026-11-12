"""An event-loop-native stdout for the SDK's `stdio_server(stdout=...)`.

By default the SDK wraps stdout with `anyio.wrap_file`, which runs every write and every
flush in a worker thread: two thread round-trips per response. `TextOut` is a plain asyncio
pipe transport with the same two-method text-file interface (`await f.write(s)`,
`await f.flush()`). It is a byte sink only: the SDK still serializes each message and adds the
newline itself, and stdin is still read by the SDK's own reader.

Like the SDK's own stdio claim, fd 1 is moved aside while serving (fd 1 -> stderr) so stray
output can never corrupt the wire.
"""

import asyncio
import os
import stat


def _is_stream(fd):
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode)


class _Sink(asyncio.Protocol):
    def __init__(self):
        self._paused = False
        self._waiters = []
        self.lost = False
        self.done = None  # future set when the transport has finished (all bytes written)

    def connection_made(self, transport):
        self.done = asyncio.get_running_loop().create_future()

    def pause_writing(self):
        self._paused = True

    def resume_writing(self):
        self._paused = False
        self._wake()

    def connection_lost(self, exc):
        self.lost = True
        self._wake()
        if self.done is not None and not self.done.done():
            self.done.set_result(None)

    def _wake(self):
        for w in self._waiters:
            if not w.done():
                w.set_result(None)
        self._waiters.clear()

    async def drain(self):
        while self._paused and not self.lost:
            w = asyncio.get_running_loop().create_future()
            self._waiters.append(w)
            await w


class TextOut:
    def __init__(self, transport, sink):
        self._transport = transport
        self._sink = sink

    async def write(self, text):
        if not self._sink.lost:
            self._transport.write(text.encode("utf-8"))

    async def flush(self):
        await self._sink.drain()

    async def aclose(self):
        """Write out anything still buffered, then close the wire (before a fast exit)."""
        self._transport.close()  # flushes the buffer first, then calls connection_lost
        if self._sink.done is not None:
            await self._sink.done


async def open_stdout():
    """Return a TextOut for the wire, or None to keep the SDK default (fd 1 is a regular
    file, which pipe transports cannot serve)."""
    if not _is_stream(1):
        return None
    wire = os.dup(1)
    os.dup2(2, 1)  # stray prints go to stderr, never onto the wire
    loop = asyncio.get_running_loop()
    transport, sink = await loop.connect_write_pipe(_Sink, os.fdopen(wire, "wb", buffering=0))
    return TextOut(transport, sink)
