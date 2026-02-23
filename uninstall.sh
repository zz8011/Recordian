#!/bin/bash
# Recordian 卸载脚本

set -e

echo "=== Recordian 卸载程序 ==="
echo ""

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 删除桌面启动器
DESKTOP_FILE="$HOME/.local/share/applications/recordian.desktop"
if [ -f "$DESKTOP_FILE" ]; then
    echo "删除桌面启动器: $DESKTOP_FILE"
    rm "$DESKTOP_FILE"
fi

# 删除启动脚本
LAUNCH_SCRIPT="$SCRIPT_DIR/recordian-launch.sh"
if [ -f "$LAUNCH_SCRIPT" ]; then
    echo "删除启动脚本: $LAUNCH_SCRIPT"
    rm "$LAUNCH_SCRIPT"
fi

echo ""
echo "=== 卸载完成！ ==="
echo ""
echo "注意："
echo "- 虚拟环境 (.venv) 未删除，如需删除请手动执行: rm -rf $SCRIPT_DIR/.venv"
echo "- 配置文件 (~/.config/recordian/) 未删除，如需删除请手动执行: rm -rf ~/.config/recordian"
echo ""
