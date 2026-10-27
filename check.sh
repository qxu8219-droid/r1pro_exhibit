#!/bin/sh
PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for PYTHON_CMD in python3 python; do
    if command -v "$PYTHON_CMD" >/dev/null 2>&1 && "$PYTHON_CMD" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$PYTHON_CMD" "$PACKAGE_DIR/check_package.py"
    fi
done
printf '%s\n' 'Python 3.9+ is required. Install Python, then try again.' >&2
exit 1
