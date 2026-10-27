#!/bin/sh
# 在文件管理器中选择“在终端中运行”，或执行 ./start_live.sh。
# 读取现有相机接口；只可按需启动独立新文件夹中的只读 VLA 采集器。
# 不调用原机器人启动脚本，不修改原文件；--no-ssh 禁止远端启动。
LIVE_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for LIVE_PYTHON in python3 python; do
    if command -v "$LIVE_PYTHON" >/dev/null 2>&1 && "$LIVE_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$LIVE_PYTHON" -B "$LIVE_PROJECT_DIR/exhibit/live_launcher.py" "$@"
    fi
done
printf '%s\n' '需要 Python 3.9+；未启动任何服务。' >&2
exit 1
