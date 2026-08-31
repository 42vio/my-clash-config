#!/bin/sh
# clash-sub integration installer bootstrap. Run as root on an apt-based Linux
# system after 3x-ui is installed with one Reality inbound on port 10443.
set -eu
[ "$(id -u)" = 0 ] || { echo "install.sh must run as root" >&2; exit 1; }
cd "$(dirname "$0")"
echo "clash-sub 安装程序"
echo "▶ [1/12] 检查基础工具"
if command -v python3 >/dev/null 2>&1 && \
   command -v curl >/dev/null 2>&1 && \
   command -v git >/dev/null 2>&1; then
    echo "↷ [1/12] 检查基础工具                 已存在"
else
    apt-get update
    apt-get install -y --no-install-recommends python3 python3-venv curl git
    echo "✓ [1/12] 检查基础工具                 已完成"
fi
echo "▶ [2/12] 创建 Python 环境"
if [ -x .venv/bin/python ]; then
    echo "↷ [2/12] 创建 Python 环境                 已存在"
else
    if ! python3 -m venv .venv; then
        apt-get update
        apt-get install -y --no-install-recommends python3-venv curl
        rm -rf .venv
        python3 -m venv .venv
    fi
    echo "✓ [2/12] 创建 Python 环境                 已完成"
fi
echo "▶ [3/12] 安装项目依赖"
.venv/bin/pip install --quiet -r requirements.txt
echo "✓ [3/12] 安装项目依赖                 已完成"
CLASH_SUB_PROGRESS_OFFSET=3 exec bin/clash-sub install
