#!/bin/sh
# 完整演示入口：包含原 --enable-motion，会实际驱动机器人。
# 仅查看画面请运行 start_live.sh；离线记录请运行 start_offline.sh。
FULL_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for FULL_PYTHON in python3 python; do
    if command -v "$FULL_PYTHON" >/dev/null 2>&1 && "$FULL_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$FULL_PYTHON" -B "$FULL_PROJECT_DIR/exhibit/full_entry.py" "$@"
    fi
done
printf '%s\n' '需要 Python 3.9+；未启动任何服务。' >&2
exit 1
