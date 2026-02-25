# Recordian 使用文档

## 目录

1. [安装](#1-安装)
2. [快速开始](#2-快速开始)
3. [配置详解](#3-配置详解)
4. [文本精炼器](#4-文本精炼器)
5. [Preset 系统](#5-preset-系统)
6. [热键配置](#6-热键配置)
7. [常见问题](#7-常见问题)

---

## 1. 安装

### 系统要求

**最低要求：**
- 操作系统：Linux（X11 或 Wayland）
- Python：3.10+
- 显存：4GB+（使用 0.6B 模型）
- 内存：8GB+

**推荐配置：**
- 操作系统：Ubuntu 22.04+ / Arch Linux
- Python：3.11+
- GPU：NVIDIA（6GB+ 显存）
- 显存：8GB+（使用 1.7B 模型）
- 内存：16GB+

**依赖软件：**
- Wayland：`wtype`
- X11：`xdotool`、`xsel` 或 `xclip`
- 托盘：`gir1.2-appindicator3-0.1`（Ubuntu/Debian）或 `libappindicator-gtk3`（Arch）
- 通知：`libnotify`（`notify-send`）

### 安装步骤

#### 方式 1：一键安装（推荐）

```bash
git clone https://github.com/zz8011/Recordian.git
cd Recordian
./install.sh
```

安装完成后，从应用菜单搜索 "Recordian" 启动，或运行 `recordian-launch.sh`。

#### 方式 2：手动安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .[qwen-asr,hotkey,gui]
```

### 下载模型

**ASR 模型（必需，约 3.5GB）：**

```bash
pip install modelscope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir ./models/Qwen3-ASR-1.7B
```

也可使用更小的 0.6B 模型（约 1.5GB，速度更快但准确率略低）：

```bash
modelscope download --model Qwen/Qwen3-ASR-0.6B --local_dir ./models/Qwen3-ASR-0.6B
```

**文本精炼模型（可选）：**

本地 transformers 模型（约 1.2GB）：

```bash
modelscope download --model Qwen/Qwen3-0.6B --local_dir ./models/Qwen3-0.6B
```

### 验证安装

```bash
recordian-tray --help
```

---

## 2. 快速开始

### 首次启动

```bash
# 启动托盘 GUI（推荐）
recordian-tray

# 或命令行模式（首次配置时使用）
recordian-hotkey-dictate \
  --asr-provider qwen-asr \
  --qwen-model ./models/Qwen3-ASR-1.7B \
  --enable-text-refine \
  --refine-model ./models/Qwen3-0.6B \
  --save-config
```

首次启动时模型加载需要 5-10 秒，请耐心等待。

### 基本录音操作

1. 按住**右 Ctrl** 开始录音（托盘出现动画）
2. 说话（椭圆随音量旋转）
3. 松开热键，自动识别并上屏
4. 按 **Ctrl+Alt+Q** 退出程序

### 切换 Preset

右键托盘图标 → Preset → 选择：
- **Default**：日常口语整理
- **Formal**：正式书面语
- **Summary**：简洁总结
- **Meeting**：会议纪要
- **Technical**：技术文档

---

## 3. 配置详解

### 配置文件位置

```
~/.config/recordian/hotkey.json
```

### 完整配置项说明

```json
{
  "asr_provider": "qwen-asr",
  "qwen_model": "models/Qwen3-ASR-1.7B",
  "asr_context": "",

  "enable_text_refine": true,
  "refine_provider": "local",
  "refine_model": "models/Qwen3-0.6B",
  "refine_preset": "default",

  "refine_api_base": "https://api.example.com",
  "refine_api_key": "your-api-key",
  "refine_api_model": "claude-3-5-sonnet-20241022",

  "refine_model_llamacpp": "/path/to/model.gguf",
  "refine_n_gpu_layers": -1,

  "hotkey": "<ctrl_r>",
  "toggle_hotkey": "<ctrl>+<space>",
  "exit_hotkey": "<ctrl>+<alt>+q",
  "trigger_mode": "ptt"
}
```

### 推荐配置

**日常使用（本地模型，快速免费）：**

```json
{
  "asr_provider": "qwen-asr",
  "qwen_model": "models/Qwen3-ASR-1.7B",
  "enable_text_refine": true,
  "refine_provider": "local",
  "refine_model": "models/Qwen3-0.6B",
  "refine_preset": "default",
  "hotkey": "<ctrl_r>",
  "trigger_mode": "ptt"
}
```

**重要场合（云端 API，高质量）：**

```json
{
  "asr_provider": "qwen-asr",
  "qwen_model": "models/Qwen3-ASR-1.7B",
  "enable_text_refine": true,
  "refine_provider": "cloud",
  "refine_api_base": "https://api.minimaxi.com/anthropic",
  "refine_api_key": "your-api-key",
  "refine_api_model": "claude-3-5-sonnet-20241022",
  "refine_preset": "meeting"
}
```

**低显存（llama.cpp，约 400MB 显存）：**

```json
{
  "asr_provider": "qwen-asr",
  "qwen_model": "models/Qwen3-ASR-1.7B",
  "enable_text_refine": true,
  "refine_provider": "llamacpp",
  "refine_model": "/path/to/qwen3-0.6b-q4_k_m.gguf",
  "refine_n_gpu_layers": -1
}
```

### 环境变量

```bash
export RECORDIAN_DEVICE=cuda          # cuda / cpu / auto
export RECORDIAN_DEBUG=1              # 调试模式
export RECORDIAN_CLIPBOARD_TIMEOUT_MS=500
export RECORDIAN_PASTE_SHORTCUT="ctrl+v"
```
