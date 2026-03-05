# 安装指南

## 系统要求

### 操作系统
- Linux (推荐 Ubuntu 20.04+)
- macOS 10.15+
- Windows 10+ (WSL2)

### Python 版本
- Python 3.10+
- Python 3.11 (推荐)
- Python 3.12

### 依赖项
- sounddevice / numpy (音频采集与处理)
- AppIndicator3 + GTK3 绑定（托盘图标）
- xdotool / xclip / libnotify（上屏与通知）
- 其他依赖见 `pyproject.toml`

---

## 安装步骤

### 1. 克隆仓库

```bash
git clone https://github.com/zz8011/Recordian.git
cd Recordian
```

### 2. 安装依赖

#### 使用 uv (推荐)

```bash
# 安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

# 安装项目依赖
uv sync --extra gui --extra hotkey --extra qwen-asr --extra wake
```

#### 使用 pip

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/macOS
# 或
.venv\Scripts\activate  # Windows

# 安装依赖
pip install -e ".[gui,hotkey,qwen-asr,wake]"
```

### 3. 系统依赖

#### Ubuntu/Debian

```bash
sudo apt-get update
sudo apt-get install -y \
    python3-gi \
    gir1.2-appindicator3-0.1 \
    xdotool \
    xclip \
    libnotify-bin
```

#### macOS

```bash
echo "当前托盘模式主要面向 Linux，macOS 建议使用命令行模式运行"
```

#### Arch Linux

```bash
sudo pacman -S python-gobject libappindicator-gtk3 xdotool xclip libnotify
```

---

## 配置

### 1. 创建配置文件

```bash
mkdir -p ~/.config/recordian
```

配置文件会在首次运行时自动创建。

### 2. 环境变量（可选）

```bash
# 日志级别
export RECORDIAN_LOG_LEVEL=INFO

# 日志文件路径
export RECORDIAN_LOG_FILE=~/.local/share/recordian/recordian.log

# Sentry DSN (错误追踪)
export SENTRY_DSN=your_sentry_dsn_here

# 环境标识
export RECORDIAN_ENV=production

# 版本号
export RECORDIAN_VERSION=0.1.0
```

---

## 验证安装

### 运行测试

```bash
# 使用 uv
uv run pytest

# 或使用 pytest
pytest
```

### 启动应用

```bash
# 托盘 GUI 模式
uv run recordian-tray

# 命令行单次录音模式
uv run recordian-linux-dictate

# 热键模式
uv run recordian-hotkey-dictate
```

---

## 故障排查

### 系统依赖缺失（托盘/通知/上屏）

**Ubuntu/Debian**:
```bash
sudo apt-get install -y python3-gi gir1.2-appindicator3-0.1 xdotool xclip libnotify-bin
```

**macOS**:
```bash
echo "当前托盘模式主要面向 Linux，macOS 建议使用命令行模式运行"
```

### 权限问题

```bash
# 添加用户到 audio 组
sudo usermod -aG audio $USER

# 重新登录生效
```

### 依赖冲突

```bash
# 清理并重新安装
rm -rf .venv
uv sync --reinstall
```

---

## 下一步

- 查看 [快速入门](Quick-Start) 开始使用
- 阅读 [配置说明](Configuration) 了解详细配置
- 参考 [故障排查](Troubleshooting) 解决常见问题

---

**最后更新**: 2026-03-01
