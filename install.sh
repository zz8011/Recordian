#!/bin/bash
# Recordian 安装脚本

set -e

show_help() {
    cat <<'EOF'
用法: ./install.sh [选项]

选项:
  --pull-external-model       安装完成后拉取外部 ASR 模型
  --external-model-id <id>    外部模型 ID（默认: Qwen/Qwen3-ASR-1.7B）
  --external-model-dir <dir>  外部模型下载目录（默认: ./models/Qwen3-ASR-1.7B）
  -h, --help                  显示帮助
EOF
}

PULL_EXTERNAL_MODEL=0
EXTERNAL_MODEL_ID="Qwen/Qwen3-ASR-1.7B"
EXTERNAL_MODEL_DIR=""

while [ $# -gt 0 ]; do
    case "$1" in
        --pull-external-model)
            PULL_EXTERNAL_MODEL=1
            shift
            ;;
        --external-model-id)
            if [ $# -lt 2 ]; then
                echo "错误: --external-model-id 需要一个参数"
                exit 1
            fi
            EXTERNAL_MODEL_ID="${2:-}"
            shift 2
            ;;
        --external-model-dir)
            if [ $# -lt 2 ]; then
                echo "错误: --external-model-dir 需要一个参数"
                exit 1
            fi
            EXTERNAL_MODEL_DIR="${2:-}"
            shift 2
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        *)
            echo "错误: 未知参数 $1"
            show_help
            exit 1
            ;;
    esac
done

echo "=== Recordian 安装程序 ==="
echo ""

# 检查 Python 版本
if ! command -v python3 &> /dev/null; then
    echo "错误: 未找到 python3，请先安装 Python 3.10+"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
echo "检测到 Python 版本: $PYTHON_VERSION"

# 检查系统依赖
echo ""
echo "检查系统依赖..."

MISSING_DEPS=()

# 检查 AppIndicator3（托盘图标）
if ! python3 -c "import gi" 2>/dev/null; then
    MISSING_DEPS+=("python3-gi")
fi

if ! python3 -c "import gi; gi.require_version('AppIndicator3', '0.1'); from gi.repository import AppIndicator3" 2>/dev/null; then
    MISSING_DEPS+=("gir1.2-appindicator3-0.1")
fi

# 检查 xdotool（X11 文本上屏）
if ! command -v xdotool &> /dev/null; then
    MISSING_DEPS+=("xdotool")
fi

# 检查 xclip（X11 剪贴板）
if ! command -v xclip &> /dev/null && ! command -v xsel &> /dev/null; then
    MISSING_DEPS+=("xclip")
fi

# 检查 notify-send（通知）
if ! command -v notify-send &> /dev/null; then
    MISSING_DEPS+=("libnotify-bin")
fi

# 如果有缺失的依赖，提示安装
if [ ${#MISSING_DEPS[@]} -gt 0 ]; then
    echo ""
    echo "⚠️  缺少以下系统依赖："
    for dep in "${MISSING_DEPS[@]}"; do
        echo "  - $dep"
    done
    echo ""
    echo "请运行以下命令安装："
    echo "  sudo apt install ${MISSING_DEPS[*]}"
    echo ""
    read -p "是否继续安装 Recordian？(y/n) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
else
    echo "✅ 所有系统依赖已安装"
fi

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$SCRIPT_DIR"
if [ -z "$EXTERNAL_MODEL_DIR" ]; then
    EXTERNAL_MODEL_DIR="$SCRIPT_DIR/models/Qwen3-ASR-1.7B"
fi

# 检查虚拟环境
if [ ! -d ".venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv .venv
fi

echo "激活虚拟环境..."
source .venv/bin/activate

echo "安装依赖..."
pip install -e ".[gui,hotkey,qwen-asr,wake]"

if [ "$PULL_EXTERNAL_MODEL" -eq 1 ]; then
    echo ""
    echo "下载外部 ASR 模型: $EXTERNAL_MODEL_ID"
    pip install modelscope
    modelscope download --model "$EXTERNAL_MODEL_ID" --local_dir "$EXTERNAL_MODEL_DIR"
    echo "✅ 外部模型下载完成: $EXTERNAL_MODEL_DIR"
else
    echo ""
    echo "可选: 拉取外部 ASR 模型（默认不拉取）"
    echo "  方式 1: 重新运行安装脚本并加参数"
    echo "    ./install.sh --pull-external-model"
    echo "  方式 2: 手动执行"
    echo "    pip install modelscope"
    echo "    modelscope download --model \"$EXTERNAL_MODEL_ID\" --local_dir \"$EXTERNAL_MODEL_DIR\""
fi

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
Exec=$SCRIPT_DIR/.venv/bin/recordian-tray
Icon=$SCRIPT_DIR/assets/logo-256.png
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
