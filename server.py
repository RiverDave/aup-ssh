import asyncio
import os
import errno
import fcntl
import struct
import termios
from pathlib import Path
import asyncssh
from aiohttp import web

ROOT = Path.home() / '.local/share/aup-ssh'

class SSHServer(asyncssh.SSHServer):
    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return dest_host in ('127.0.0.1', '::1')

async def shell(process):
    if process.term_type:
        await terminal(process)
        return
    child = await asyncio.create_subprocess_exec(
        '/bin/bash', '-lc', process.command or 'exec bash -i',
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE)
    async def incoming():
        try:
            while data := await process.stdin.read(65536):
                child.stdin.write(data)
                await child.stdin.drain()
        finally:
            child.stdin.close()
    async def outgoing(source, dest):
        while data := await source.read(65536):
            dest.write(data)
    tasks = [asyncio.create_task(incoming()),
             asyncio.create_task(outgoing(child.stdout, process.stdout)),
             asyncio.create_task(outgoing(child.stderr, process.stderr))]
    try:
        code = await child.wait()
        await asyncio.gather(*tasks[1:])
        process.exit(code)
    finally:
        for task in tasks:
            task.cancel()
        if child.returncode is None:
            child.kill()

async def terminal(process):
    master, slave = os.openpty()
    width, height, _, _ = process.term_size
    def resize(width, height):
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
    resize(width or 80, height or 24)
    def setup():
        os.setsid()
        fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    child = await asyncio.create_subprocess_exec(
        '/bin/bash', '-lc', process.command or 'exec bash -i',
        stdin=slave, stdout=slave, stderr=slave,
        env={**os.environ, 'TERM': process.term_type}, preexec_fn=setup)
    os.close(slave)
    loop = asyncio.get_running_loop()
    queue = asyncio.Queue()
    def readable():
        try:
            data = os.read(master, 65536)
        except OSError as exc:
            if exc.errno != errno.EIO:
                raise
            data = b''
        queue.put_nowait(data)
        if not data:
            loop.remove_reader(master)
    loop.add_reader(master, readable)
    async def incoming():
        while True:
            try:
                data = await process.stdin.read(65536)
            except asyncssh.TerminalSizeChanged as exc:
                resize(exc.width, exc.height)
                continue
            if not data:
                os.write(master, b'\x04')
                return
            os.write(master, data)
    async def outgoing():
        while data := await queue.get():
            process.stdout.write(data)
    tasks = [asyncio.create_task(incoming()), asyncio.create_task(outgoing())]
    try:
        code = await child.wait()
        await tasks[1]
        process.exit(code)
    finally:
        for task in tasks:
            task.cancel()
        loop.remove_reader(master)
        os.close(master)
        if child.returncode is None:
            child.kill()

async def bridge(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    reader, writer = await asyncio.open_connection('127.0.0.1', 2222)
    async def upstream():
        async for msg in ws:
            if msg.type == web.WSMsgType.BINARY:
                writer.write(msg.data)
                await writer.drain()
    async def downstream():
        while data := await reader.read(65536):
            await ws.send_bytes(data)
    tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        writer.close()
        await ws.close()
    return ws

async def main():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    hostkey = ROOT / 'host_ed25519'
    if not hostkey.exists():
        asyncssh.generate_private_key('ssh-ed25519').write_private_key(hostkey)
        hostkey.chmod(0o600)
    await asyncssh.listen('127.0.0.1', 2222,
        server_factory=SSHServer,
        server_host_keys=[str(hostkey)],
        authorized_client_keys=str(ROOT / 'authorized_keys'),
        process_factory=shell, encoding=None,
        sftp_factory=asyncssh.SFTPServer,
        allow_scp=True)
    app = web.Application()
    app.router.add_get('/ssh', bridge)
    app.router.add_get('/health', lambda request: web.Response(text='aup-ssh-ready'))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', 8765).start()
    print('AUP_SSH_READY ' + asyncssh.read_private_key(hostkey).export_public_key().decode().strip(), flush=True)
    await asyncio.Future()

if __name__ == '__main__':
    asyncio.run(main())
