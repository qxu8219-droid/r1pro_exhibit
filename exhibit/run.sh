#!/bin/sh
# Linux file managers may ask: choose "Run in Terminal".
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for PYTHON_CMD in python3 python; do
    if command -v "$PYTHON_CMD" >/dev/null 2>&1 && "$PYTHON_CMD" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$PYTHON_CMD" "$SCRIPT_DIR/launcher.py" "$@"
    fi
done
printf '%s\n' 'Python 3.9+ is required. Install Python, then open this launcher again.' >&2
if [ -t 0 ]; then
    printf '%s' 'Press Enter to close...'
    read -r REPLY
fi
exit 1
