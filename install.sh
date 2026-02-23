#!/bin/bash
# Recordian 安装脚本

set -e

echo "=== Recordian 安装程序 ==="
echo ""

# 检查 Python 版本
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "检测到 Python 版本: $PYTHON_VERSION"

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv .venv
fi

echo "激活虚拟环境..."
source .venv/bin/activate

echo "安装依赖..."
pip install -e .[gui,hotkey,qwen-asr]

# 创建桌面启动器
DESKTOP_FILE="$HOME/.local/share/applications/recordian.desktop"
echo "创建桌面启动器: $DESKTOP_FILE"

mkdir -p "$HOME/.local/share/applications"

cat > "$DESKTOP_FILE" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Recordian
Comment=Linux 语音输入助手
Exec=$SCRIPT_DIR/.venv/bin/python -m recordian.tray_gui
Icon=$SCRIPT_DIR/assets/logo.svg
Terminal=false
Categories=Utility;Audio;
StartupNotify=false
EOF

chmod +x "$DESKTOP_FILE"

# 创建快速启动脚本
LAUNCH_SCRIPT="$SCRIPT_DIR/recordian-launch.sh"
echo "创建启动脚本: $LAUNCH_SCRIPT"

cat > "$LAUNCH_SCRIPT" << 'EOF'
#!/bin/bash
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"
source .venv/bin/activate
recordian-tray
EOF

chmod +x "$LAUNCH_SCRIPT"

echo ""
echo "=== 安装完成！ ==="
echo ""
echo "启动方式："
echo "1. 从应用菜单搜索 'Recordian' 启动"
echo "2. 运行: $LAUNCH_SCRIPT"
echo "3. 命令行: cd $SCRIPT_DIR && source .venv/bin/activate && recordian-tray"
echo ""
echo "配置文件位置: ~/.config/recordian/"
echo ""
