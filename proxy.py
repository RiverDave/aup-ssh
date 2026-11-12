import asyncio
import base64
import json
import os
import re
import signal
import sys
from http.cookies import SimpleCookie
from pathlib import Path
import aiohttp
from yarl import URL

ROOT = Path(__file__).resolve().parent
BASE = 'https://dub.aupcloud.io/user/github%3Ariverdave'
# Relay gives up after this long without input; ssh_config's 30s keepalive keeps live sessions under it.
RELAY_IDLE_SECONDS = 120

class PortalError(Exception):
    pass

def cookie_jar(records):
    jar = aiohttp.CookieJar()
    for record in records:
        cookie = SimpleCookie()
        cookie.load(record['name'] + '=' + record['value'])
        morsel = cookie[record['name']]
        morsel['domain'] = record['domain']
        morsel['path'] = URL(record.get('path', '/'), encoded=True).path
        if record.get('secure'):
            morsel['secure'] = True
        jar.update_cookies(cookie, response_url=URL('https://dub.aupcloud.io'))
    return jar

async def check_workspace(session, origin='https://dub.aupcloud.io'):
    origin = URL(origin)
    async def fetch(path):
        url = URL(str(origin) + path, encoded=True)
        for _ in range(12):
            if url.origin() != origin:
                raise PortalError('AUP authentication needs refreshing in the browser.')
            if url.path.startswith(('/hub/login', '/hub/oauth_login')):
                raise PortalError('AUP authentication expired; refresh the saved portal login.')
            if url.path.startswith(('/hub/spawn', '/hub/user/')):
                raise PortalError('AUP workspace is stopped or starting. Start it in the portal; no compute was launched.')
            async with session.get(url, allow_redirects=False) as response:
                if response.status in (301, 302, 303, 307, 308):
                    url = url.join(URL(response.headers['Location'], encoded=True))
                    continue
                if response.status in (401, 403):
                    raise PortalError('AUP authentication needs refreshing in the browser.')
                if response.status >= 400:
                    raise PortalError(f'AUP workspace unavailable (HTTP {response.status}).')
                return await response.text()
        raise PortalError('AUP authentication redirect did not complete.')
    home = await fetch('/hub/home')
    if re.search(r'''\bid=["']start["']''', home):
        raise PortalError('AUP workspace is stopped. Start it in the portal; no compute was launched.')
    await fetch('/user/github%3Ariverdave/')

async def jupyter_proxy(session, reader, base=BASE):
    cookies = session.cookie_jar.filter_cookies(URL(base + '/', encoded=True))
    xsrf = cookies.get('_xsrf')
    headers = {'X-XSRFToken': xsrf.value if xsrf else '', 'Origin': 'https://dub.aupcloud.io'}
    async with session.post(URL(base + '/api/terminals', encoded=True), json={}, headers=headers) as response:
        response.raise_for_status()
        name = (await response.json())['name']
    code = f'''import base64,os,select,socket,sys,tty
tty.setraw(0)
s=socket.create_connection(('127.0.0.1',2222))
print('AUPREADY',flush=True)
while True:
 ready,_,_=select.select([s,sys.stdin.buffer],[],[],{RELAY_IDLE_SECONDS})
 if not ready: break
 if s in ready:
  data=s.recv(65536)
  if not data: break
  print('AUPDATA:'+base64.b64encode(data).decode(),flush=True)
 if sys.stdin.buffer in ready:
  line=sys.stdin.buffer.readline()
  if not line: break
  s.sendall(base64.b64decode(line))
'''
    command = "exec python3 -u -c \"import base64; exec(base64.b64decode('" + base64.b64encode(code.encode()).decode() + "'))\"\r"
    try:
        url = base.replace('https:', 'wss:') + '/terminals/websocket/' + name
        async with session.ws_connect(URL(url, encoded=True), headers=headers, max_msg_size=0) as ws:
            shell_ready = asyncio.Event()
            started = asyncio.Event()
            async def send():
                await asyncio.wait_for(shell_ready.wait(), 10)
                await ws.send_json(['stdin', command])
                await asyncio.wait_for(started.wait(), 15)
                while data := await reader.read(65536):
                    await ws.send_json(['stdin', base64.b64encode(data).decode() + '\n'])
            async def receive():
                buffer = ''
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    item = json.loads(msg.data)
                    if item[0] != 'stdout':
                        continue
                    buffer += item[1]
                    if not shell_ready.is_set() and ('$ ' in buffer or '# ' in buffer):
                        shell_ready.set()
                    while '\n' in buffer:
                        line, buffer = buffer.split('\n', 1)
                        if 'AUPREADY' in line:
                            started.set()
                        elif started.is_set() and line.startswith('AUPDATA:'):
                            data = memoryview(base64.b64decode(line[8:]))
                            while data:
                                data = data[os.write(1, data):]
            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    finally:
        async with session.delete(URL(base + '/api/terminals/' + name, encoded=True), headers=headers) as response:
            if response.status != 404:  # already gone after the relay's idle timeout
                response.raise_for_status()

async def main():
    # ssh sends SIGHUP to its ProxyCommand on exit; cancel so the terminal still gets deleted
    task = asyncio.current_task()
    for sig in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, task.cancel)
    records = json.loads((ROOT / 'cookies.json').read_text())
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    transport, _ = await asyncio.get_running_loop().connect_read_pipe(lambda: protocol, sys.stdin.buffer)
    async with aiohttp.ClientSession(cookie_jar=cookie_jar(records)) as session:
        await check_workspace(session)
        try:
            ws = await session.ws_connect(
            URL('wss://dub.aupcloud.io/user/github%3Ariverdave/proxy/8765/ssh', encoded=True),
            origin='https://dub.aupcloud.io', max_msg_size=0)
        except aiohttp.WSServerHandshakeError as exc:
            if exc.request_info.real_url.path.startswith('/hub/login'):
                raise PortalError('AUP authentication needs refreshing in the browser.') from None
            if exc.status != 404:
                raise
            await jupyter_proxy(session, reader)
            transport.close()
            return
        async with ws:
            async def send():
                while data := await reader.read(65536):
                    await ws.send_bytes(data)
            async def receive():
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.BINARY:
                        data = memoryview(msg.data)
                        while data:
                            data = data[os.write(1, data):]
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        raise ws.exception()
            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                task.result()
    transport.close()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except asyncio.CancelledError:
        pass
    except PortalError as exc:
        print(f'aup-ssh: {exc}', file=sys.stderr)
        raise SystemExit(1)
    except (aiohttp.ClientError, TimeoutError, OSError) as exc:
        print(f'aup-ssh: connection failed ({type(exc).__name__}); check portal login, workspace state and SSH bridge.', file=sys.stderr)
        raise SystemExit(1)
