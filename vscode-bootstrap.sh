set -eu
python3 - <<'PY'
import hashlib
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import urllib.request

root = Path.home() / '.local/share/aup-ssh'
source = root / 'server.py.vscode-new'
target = root / 'server.py'
expected = 'f0f5f4a7e19707df6ada7cdedc21b240e18c3ebb445961ee542181020eb4bb97'
assert hashlib.sha256(source.read_bytes()).hexdigest() == expected
pids = []
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():
        continue
    try:
        args = (entry / 'cmdline').read_bytes().split(b'\0')
        if len(args) > 1 and args[1].decode() == str(target):
            pids.append(int(entry.name))
    except (OSError, UnicodeError):
        continue
if len(pids) != 1:
    raise SystemExit(f'Expected exactly one existing SSH bridge; found {pids}')
backup = root / ('server.py.pre-vscode-' + time.strftime('%Y%m%d-%H%M%S'))
shutil.copy2(target, backup)
os.replace(source, target)
os.kill(pids[0], signal.SIGTERM)
for _ in range(40):
    if not Path('/proc', str(pids[0])).exists():
        break
    time.sleep(0.1)
with (root / 'server.log').open('ab', buffering=0) as log:
    child = subprocess.Popen([sys.executable, str(target)], stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True)
for _ in range(50):
    if child.poll() is not None:
        raise SystemExit(f'New SSH bridge exited {child.returncode}; preserved backup {backup}')
    try:
        with urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=1) as response:
            assert response.read().decode() == 'aup-ssh-ready'
            print('BRIDGE_READY', child.pid)
            print('SERVER_SHA256', hashlib.sha256(target.read_bytes()).hexdigest())
            print('BACKUP', backup)
            break
    except OSError:
        time.sleep(0.2)
else:
    raise SystemExit('SSH bridge did not become healthy')
PY
