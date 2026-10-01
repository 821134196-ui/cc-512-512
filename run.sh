#!/usr/bin/env bash
# 单条本地命令启动（自动准备数据目录，首次运行自动建库与过热样例）
set -euo pipefail
cd "$(dirname "$0")"
exec python3 app.py "$@"
