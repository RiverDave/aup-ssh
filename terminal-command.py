"""Run a bounded setup command through an existing AUP Jupyter terminal."""

import argparse
import asyncio
import base64
import json
import re
import sys
import uuid

import aiohttp
from yarl import URL

from proxy import ROOT, check_workspace, cookie_jar


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command_file")
    parser.add_argument("--timeout", type=int, default=60)
    args = parser.parse_args()
    from pathlib import Path

    script = Path(args.command_file).read_bytes()
    marker = "AUP_COMMAND_RESULT_" + uuid.uuid4().hex
    wrapper = (
        "import base64,subprocess; "
        f"p=subprocess.run(['bash','-c',base64.b64decode({base64.b64encode(script).decode()!r}).decode()]); "
        f"print('\\n{marker} '+str(p.returncode),flush=True)"
    )
    encoded = base64.b64encode(wrapper.encode()).decode()
    command = f"python3 -u -c \"import base64;exec(base64.b64decode('{encoded}'))\"\r"
    base = "https://dub.aupcloud.io/user/github%3Ariverdave"
    records = json.loads((ROOT / "cookies.json").read_text())
    async with aiohttp.ClientSession(cookie_jar=cookie_jar(records)) as session:
        await check_workspace(session)
        cookies = session.cookie_jar.filter_cookies(URL(base + "/", encoded=True))
        xsrf = cookies.get("_xsrf")
        headers = {
            "X-XSRFToken": xsrf.value if xsrf else "",
            "Origin": "https://dub.aupcloud.io",
        }
        async with session.post(URL(base + "/api/terminals", encoded=True), json={}, headers=headers) as response:
            response.raise_for_status()
            name = (await response.json())["name"]
        try:
            url = base.replace("https:", "wss:") + "/terminals/websocket/" + name
            async with session.ws_connect(URL(url, encoded=True), headers=headers) as ws:
                async with asyncio.timeout(args.timeout):
                    initial = ""
                    while "$ " not in initial and "# " not in initial:
                        message = await ws.receive()
                        if message.type != aiohttp.WSMsgType.TEXT:
                            raise RuntimeError("Terminal closed before its shell was ready")
                        event = json.loads(message.data)
                        if event[0] == "stdout":
                            initial += event[1]
                    await ws.send_json(["stdin", command])
                    output = ""
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        event = json.loads(message.data)
                        if event[0] != "stdout":
                            continue
                        output += event[1]
                        match = re.search(r"(?:\r?\n)" + marker + r" (\d+)\r?\n", output)
                        if match:
                            lines = output[:match.start()].splitlines()
                            # The first line is the encoded shell command, not its output.
                            print("\n".join(lines[1:]))
                            print("REMOTE_EXIT", match.group(1))
                            return int(match.group(1))
                    raise RuntimeError("Terminal closed before command completion")
        finally:
            async with session.delete(URL(base + "/api/terminals/" + name, encoded=True), headers=headers) as response:
                response.raise_for_status()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
