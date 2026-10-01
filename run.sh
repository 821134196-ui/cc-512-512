#!/usr/bin/env bash
# 单条本地命令启动叉车换电管理系统（仅依赖 Python 3 标准库）
#   ./run.sh            首次启动：播种演示数据（含过热样例）
#   SWAP_RESET=1 ./run.sh  重建演示数据
set -euo pipefail
cd "$(dirname "$0")"

PORT="${SWAP_PORT:-8000}"
export SWAP_PORT="$PORT"

echo "──────────────────────────────────────────────"
echo " 叉车换电管理系统"
echo " 地址: http://localhost:${PORT}"
echo " 过热样例: BAT-A03(充电中升温) / BAT-B04(57.2℃已隔离)"
echo " 角色: 调度员 / 换电人 / 维修人员（右上角切换）"
echo " Ctrl+C 退出"
echo "──────────────────────────────────────────────"

exec python3 app.py
