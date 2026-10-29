#!/bin/sh
# 网络不稳定时使用：原真实记录主屏、车队模拟及模型推理回放。
# 不读取 live_settings.json，不执行 SSH，不接收机器人相机或 VLA 数据。
OFFLINE_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
OFFLINE_DB_PATH="$HOME/r1pro_live/offline/fleet.sqlite3"
for OFFLINE_PYTHON in python3 python; do
    if command -v "$OFFLINE_PYTHON" >/dev/null 2>&1 && "$OFFLINE_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$OFFLINE_PYTHON" -B "$OFFLINE_PROJECT_DIR/exhibit/launcher.py" --db "$OFFLINE_DB_PATH" "$@"
    fi
done
printf '%s\n' '需要 Python 3.9+；未启动任何服务。' >&2
exit 1
