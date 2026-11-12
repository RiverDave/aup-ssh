set -e
printf 'AUP_NATIVE_TERMINAL_OK\n'
python3 -c 'import os; print("REMOTE_HOME", os.path.expanduser("~"))'
