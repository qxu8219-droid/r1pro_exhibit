#!/bin/sh
# 在文件管理器中选择“在终端中运行”，或执行 ./start_live.sh。
# 自动读取 live_settings.json 并只读测量机器人时差。
# 按配置检查相机桥与只读VLA采集器；缺少时通过独立新目录的启动器恢复。
# 仅调用已有相机桥，不启动技能或运动、不修改原文件；--no-ssh 禁止远端启动。
LIVE_PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd) || exit 1
for LIVE_PYTHON in python3 python; do
    if command -v "$LIVE_PYTHON" >/dev/null 2>&1 && "$LIVE_PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$LIVE_PYTHON" -B "$LIVE_PROJECT_DIR/exhibit/live_entry.py" "$@"
    fi
done
printf '%s\n' '需要 Python 3.9+；未启动任何服务。' >&2
exit 1
