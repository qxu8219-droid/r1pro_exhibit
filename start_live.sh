#!/bin/sh
# 在文件管理器中选择“在终端中运行”，或执行 ./start_live.sh。
# 相机接口未运行时，通过 SSH 执行机器人已有启动脚本，再接收画面并展示。
# 不保存密码、不修改机器人源码或配置；--no-ssh 可只接收已有画面。
LIVE_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for LIVE_PYTHON in python3 python; do
    if command -v "$LIVE_PYTHON" >/dev/null 2>&1 && "$LIVE_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$LIVE_PYTHON" -B "$LIVE_PROJECT_DIR/exhibit/live_launcher.py" "$@"
    fi
done
printf '%s\n' '需要 Python 3.9+；未启动任何服务。' >&2
exit 1
