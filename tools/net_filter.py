"""Host filter for agent sessions: a plain HTTPS CONNECT tunnel on 127.0.0.1:8899. It sees hostnames only.

It never decrypts traffic, so it cannot see the login token or any content. Logs host and allow/deny.
  python3 net_filter.py <log-file>
"""
import asyncio, sys, time

ALLOW = {'api.anthropic.com'}
log = open(sys.argv[1], 'a', buffering=1)


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        w.close()


async def handle(r, w):
    try:
        head = await r.readuntil(b'\r\n\r\n')
        parts = head.split(b'\r\n')[0].decode('latin1').split()
        host, _, port = parts[1].partition(':')
        ok = parts[0] == 'CONNECT' and host in ALLOW and port == '443'
        log.write(f'{time.strftime("%H:%M:%S")} {"allow" if ok else "deny "} {parts[0]} {parts[1][:120]}\n')
        if not ok:
            w.write(b'HTTP/1.1 403 Forbidden\r\n\r\n')
            await w.drain()
            w.close()
            return
        ur, uw = await asyncio.open_connection(host, 443)
        w.write(b'HTTP/1.1 200 OK\r\n\r\n')
        await w.drain()
        await asyncio.gather(pipe(r, uw), pipe(ur, w))
    except (ConnectionError, asyncio.IncompleteReadError, IndexError, asyncio.LimitOverrunError):
        w.close()


async def main():
    async with await asyncio.start_server(handle, '127.0.0.1', 8899) as s:
        await s.serve_forever()

asyncio.run(main())
