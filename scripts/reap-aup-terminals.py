"""Reap leaked Jupyter terminals created by the AUP SSH bridge (native-terminal fallback).

Each SSH invocation via proxy.py's fallback creates one terminal and deletes it in a
`finally`; killed masters and broken bridges skip that delete, so dozens accumulate and
slow later connects. Confirm the SSH service is reparented (`ppid 1`) before reaping:
    ps -eww -o pid,ppid,etime,args | grep server.py

Usage: <aup-cloud venv python> scripts/reap-aup-terminals.py [--dry-run]
Never prints cookies or XSRF values.
"""
import asyncio
import json
import sys
from pathlib import Path

AUP = Path.home() / '.local/share/aup-cloud'
sys.path.insert(0, str(AUP))
from proxy import ROOT, check_workspace, cookie_jar  # noqa: E402

import aiohttp  # noqa: E402
from yarl import URL  # noqa: E402

BASE = 'https://dub.aupcloud.io/user/github%3Ariverdave'


async def main(dry_run: bool) -> None:
    records = json.loads((ROOT / 'cookies.json').read_text())
    async with aiohttp.ClientSession(cookie_jar=cookie_jar(records)) as session:
        await check_workspace(session)
        cookies = session.cookie_jar.filter_cookies(URL(BASE + '/', encoded=True))
        xsrf = cookies.get('_xsrf')
        headers = {'X-XSRFToken': xsrf.value if xsrf else '', 'Origin': 'https://dub.aupcloud.io'}
        async with session.get(URL(BASE + '/api/terminals', encoded=True)) as response:
            names = [t['name'] for t in await response.json()]
        print(f'found {len(names)} terminal(s) on the pod')
        if dry_run:
            print('dry run: nothing deleted')
            return
        gone = failed = 0
        for name in names:
            async with session.delete(URL(BASE + '/api/terminals/' + name, encoded=True),
                                      headers=headers) as response:
                await response.read()
                if response.status in (200, 204):
                    gone += 1
                else:
                    failed += 1
        async with session.get(URL(BASE + '/api/terminals', encoded=True)) as response:
            left = [t['name'] for t in await response.json()]
        print(f'deleted {gone}, failed {failed}, remaining {len(left)}')
        print('now run one ssh command to prove the bridge survived')


if __name__ == '__main__':
    asyncio.run(main('--dry-run' in sys.argv))
