#!/bin/bash
# Recordian 服务器一键部署脚本
# 用于在 192.168.5.225 上部署 ASR + LLM 服务

set -e

echo "=== Recordian 服务器部署脚本 ==="
echo ""

# 检查是否为 root
if [ "$EUID" -ne 0 ]; then
    echo "⚠️  需要 root 权限来安装系统服务"
    echo "请使用: sudo $0"
    exit 1
fi

# 获取脚本所在目录
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "项目目录: $PROJECT_ROOT"
echo ""

# 步骤 1: 安装 Ollama
echo "=== 步骤 1: 安装 Ollama ==="
if command -v ollama &> /dev/null; then
    echo "✅ Ollama 已安装"
else
    echo "正在安装 Ollama..."
    curl -fsSL https://ollama.com/install.sh | sh
    echo "✅ Ollama 安装完成"
fi
echo ""

# 步骤 2: 配置 Ollama 服务
echo "=== 步骤 2: 配置 Ollama 服务 ==="
OLLAMA_SERVICE="/etc/systemd/system/ollama.service"

if [ -f "$OLLAMA_SERVICE" ]; then
    echo "配置 Ollama 监听所有网络接口..."

    # 备份原配置
    cp "$OLLAMA_SERVICE" "$OLLAMA_SERVICE.bak"

    # 添加环境变量
    if ! grep -q "Environment=\"OLLAMA_HOST=0.0.0.0:11434\"" "$OLLAMA_SERVICE"; then
        sed -i '/\[Service\]/a Environment="OLLAMA_HOST=0.0.0.0:11434"' "$OLLAMA_SERVICE"
        echo "✅ Ollama 配置已更新"
    else
        echo "✅ Ollama 已配置为监听所有接口"
    fi

    # 重启服务
    systemctl daemon-reload
    systemctl restart ollama
    systemctl enable ollama
    echo "✅ Ollama 服务已重启"
else
    echo "⚠️  未找到 Ollama systemd 服务文件"
    echo "手动启动 Ollama: OLLAMA_HOST=0.0.0.0:11434 ollama serve"
fi
echo ""

# 步骤 3: 下载 Ollama 模型
echo "=== 步骤 3: 下载 Ollama 模型 ==="
echo "正在下载 Qwen2.5:7b..."
ollama pull qwen2.5:7b
echo "✅ Qwen2.5:7b 下载完成"
echo ""

# 步骤 4: 检查 Python 环境
echo "=== 步骤 4: 检查 Python 环境 ==="
cd "$PROJECT_ROOT"

if [ ! -d ".venv" ]; then
    echo "创建虚拟环境..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "安装依赖..."
pip install -e .[qwen-asr] flask

echo "✅ Python 环境准备完成"
echo ""

# 步骤 5: 下载 ASR 模型
echo "=== 步骤 5: 检查 ASR 模型 ==="
ASR_MODEL_PATH="$PROJECT_ROOT/models/Qwen3-ASR-1.7B"

if [ -d "$ASR_MODEL_PATH" ]; then
    echo "✅ ASR 模型已存在: $ASR_MODEL_PATH"
else
    echo "⚠️  ASR 模型不存在，需要下载"
    echo "请运行以下命令下载模型："
    echo "  pip install modelscope"
    echo "  modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir $ASR_MODEL_PATH"
    echo ""
    read -p "是否现在下载？(y/n) " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        pip install modelscope
        modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir "$ASR_MODEL_PATH"
        echo "✅ ASR 模型下载完成"
    else
        echo "⚠️  跳过模型下载，请稍后手动下载"
    fi
fi
echo ""

# 步骤 6: 创建 systemd 服务
echo "=== 步骤 6: 创建 ASR 服务 ==="

# 获取当前用户（实际运行 sudo 的用户）
ACTUAL_USER="${SUDO_USER:-$USER}"
ACTUAL_HOME=$(eval echo ~$ACTUAL_USER)

cat > /etc/systemd/system/recordian-asr.service << EOF
[Unit]
Description=Recordian ASR HTTP Server
After=network.target

[Service]
Type=simple
User=$ACTUAL_USER
WorkingDirectory=$PROJECT_ROOT
Environment="PATH=$PROJECT_ROOT/.venv/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$PROJECT_ROOT/.venv/bin/python $SCRIPT_DIR/asr_server.py --host 0.0.0.0 --port 8000 --model $ASR_MODEL_PATH
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

echo "✅ systemd 服务文件已创建"
echo ""

# 步骤 7: 启动服务
echo "=== 步骤 7: 启动服务 ==="
systemctl daemon-reload
systemctl enable recordian-asr
systemctl start recordian-asr

echo "✅ ASR 服务已启动"
echo ""

# 步骤 8: 检查服务状态
echo "=== 步骤 8: 检查服务状态 ==="
echo ""
echo "Ollama 服务状态："
systemctl status ollama --no-pager | head -10
echo ""
echo "ASR 服务状态："
systemctl status recordian-asr --no-pager | head -10
echo ""

# 步骤 9: 测试服务
echo "=== 步骤 9: 测试服务 ==="
echo ""
echo "等待服务启动..."
sleep 5

echo "测试 Ollama 服务..."
if curl -s http://localhost:11434/api/tags > /dev/null; then
    echo "✅ Ollama 服务正常"
else
    echo "❌ Ollama 服务异常"
fi

echo "测试 ASR 服务..."
if curl -s http://localhost:8000/health > /dev/null; then
    echo "✅ ASR 服务正常"
else
    echo "❌ ASR 服务异常（可能还在加载模型）"
fi
echo ""

# 完成
echo "=== 部署完成！ ==="
echo ""
echo "服务信息："
echo "  - Ollama LLM: http://192.168.5.225:11434"
echo "  - ASR 服务:   http://192.168.5.225:8000"
echo ""
echo "管理命令："
echo "  - 查看 ASR 日志: sudo journalctl -u recordian-asr -f"
echo "  - 重启 ASR:     sudo systemctl restart recordian-asr"
echo "  - 停止 ASR:     sudo systemctl stop recordian-asr"
echo "  - 查看 Ollama:  sudo systemctl status ollama"
echo ""
echo "客户端配置示例："
echo "  ~/.config/recordian/hotkey.json"
echo ""
echo "  {"
echo "    \"asr_provider\": \"http-cloud\","
echo "    \"asr_endpoint\": \"http://192.168.5.225:8000/transcribe\","
echo "    \"asr_timeout_s\": 30,"
echo ""
echo "    \"enable_text_refine\": true,"
echo "    \"refine_provider\": \"cloud\","
echo "    \"refine_api_base\": \"http://192.168.5.225:11434\","
echo "    \"refine_api_key\": \"dummy\","
echo "    \"refine_api_model\": \"qwen2.5:7b\""
echo "  }"
echo ""
echo "提示：连接 Ollama 时 refine_api_base 不要加 /v1"
echo ""
