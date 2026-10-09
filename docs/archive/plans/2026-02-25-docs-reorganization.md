# 文档整理实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 删除旧文档，新建 `docs/USER_GUIDE.md` 综合使用文档，简化 README.md 并添加链接。

**Architecture:** 单一综合文档（线性叙述式），README 作为入口页面，USER_GUIDE 包含完整使用说明。

**Tech Stack:** Markdown

---

### Task 1: 删除旧文档文件

**Files:**
- Delete: `QUICKSTART.md`
- Delete: `INSTALL.md`
- Delete: `docs/llamacpp-guide.md`
- Delete: `docs/LLAMACPP_GUIDE.md`

**Step 1: 删除文件**

```bash
rm QUICKSTART.md INSTALL.md docs/llamacpp-guide.md docs/LLAMACPP_GUIDE.md
```

**Step 2: 确认删除**

```bash
ls QUICKSTART.md INSTALL.md docs/llamacpp-guide.md docs/LLAMACPP_GUIDE.md 2>&1
```

Expected: `No such file or directory`（4 个文件均不存在）

**Step 3: Commit**

```bash
git add -A
git commit -m "docs: 删除旧文档文件（将合并至 USER_GUIDE）"
```

---

### Task 2: 创建 docs/USER_GUIDE.md（第 1-3 节）

**Files:**
- Create: `docs/USER_GUIDE.md`

**Step 1: 写入文件头部和第 1-3 节**

创建 `docs/USER_GUIDE.md`，内容如下（第 1-3 节）：

```markdown
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

  "refine_provider_cloud": "cloud",
  "refine_api_base": "https://api.example.com",
  "refine_api_key": "your-api-key",
  "refine_api_model": "claude-3-5-sonnet-20241022",

  "refine_provider_llamacpp": "llamacpp",
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
```

**Step 2: 确认文件已创建**

```bash
wc -l docs/USER_GUIDE.md
```

Expected: 行数大于 100

**Step 3: Commit**

```bash
git add docs/USER_GUIDE.md
git commit -m "docs: 新建 USER_GUIDE.md（安装、快速开始、配置详解）"
```

---

### Task 3: 补充 docs/USER_GUIDE.md（第 4-7 节）

**Files:**
- Modify: `docs/USER_GUIDE.md`

**Step 1: 追加第 4-7 节内容**

在 `docs/USER_GUIDE.md` 末尾追加以下内容：

```markdown
---

## 4. 文本精炼器

Recordian 支持三种文本精炼后端，可按需选择。

### 4.1 本地 Qwen3（transformers）

使用 Qwen3-0.6B 模型，完全本地运行。

**安装：**

```bash
pip install transformers torch
modelscope download --model Qwen/Qwen3-0.6B --local_dir ./models/Qwen3-0.6B
```

**配置：**

```json
{
  "refine_provider": "local",
  "refine_model": "models/Qwen3-0.6B"
}
```

**特点：** 速度约 3s，显存约 1.5GB，完全本地，免费。

### 4.2 llama.cpp（GGUF 量化）

使用 GGUF 量化模型，显存占用更低，速度更快。

**安装 llama-cpp-python（带 CUDA）：**

```bash
CMAKE_ARGS="-DLLAMA_CUDA=on" pip install llama-cpp-python
```

**转换模型为 GGUF 格式：**

```bash
git clone https://github.com/ggerganov/llama.cpp
cd llama.cpp && make

# 转换为 FP16
python convert_hf_to_gguf.py /path/to/Qwen3-0.6B --outtype f16

# 量化为 Q4_K_M（推荐）
./llama-quantize ggml-model-f16.gguf qwen3-0.6b-q4_k_m.gguf Q4_K_M
```

量化选项：
- **Q4_K_M**：推荐，平衡速度和质量，约 400MB
- **Q5_K_M**：质量更高，约 500MB
- **Q8_0**：接近原始质量，约 800MB

**配置：**

```json
{
  "refine_provider": "llamacpp",
  "refine_model": "/path/to/qwen3-0.6b-q4_k_m.gguf",
  "refine_n_gpu_layers": -1
}
```

`refine_n_gpu_layers` 说明：
- `-1`：全部层放 GPU（推荐，显存充足时）
- `0`：全部用 CPU
- `10`：部分层放 GPU（显存不足时）

**性能对比：**

| 方案 | 速度 | 显存 | 推荐场景 |
|------|------|------|---------|
| transformers | ~3s | ~1.5GB | 质量优先 |
| llama.cpp Q4 | ~1s | ~400MB | 速度/显存优先 |

### 4.3 云端 LLM（OpenAI 兼容）

支持任何 OpenAI 兼容的 API（Claude、GPT、MiniMax 等）。

**配置：**

```json
{
  "refine_provider": "cloud",
  "refine_api_base": "https://api.example.com",
  "refine_api_key": "your-api-key",
  "refine_api_model": "claude-3-5-sonnet-20241022"
}
```

**特点：** 质量最高，需要网络，按量付费。

---

## 5. Preset 系统

### 内置 Preset

| Preset | 用途 | 效果示例 |
|--------|------|---------|
| `default` | 日常口语整理 | 去除语气词、重复词，添加标点 |
| `formal` | 正式书面语 | 口语 → 书面语转换 |
| `summary` | 简洁总结 | 提炼核心内容 |
| `meeting` | 会议纪要 | 整理为列表格式 |
| `technical` | 技术文档 | 保留技术术语，结构清晰 |

### 自定义 Preset

在 `presets/` 目录下创建 `.md` 文件：

```markdown
# 我的自定义预设

将以下口语整理为代码注释风格：
- 简洁明了
- 使用技术术语
- 数字使用阿拉伯数字

原文：{text}
```

保存为 `presets/my-preset.md`，然后在配置中使用：

```json
{
  "refine_preset": "my-preset"
}
```

**关键词触发机制（仅 llama.cpp 后端）：**

Preset 文件中的关键词会影响 Few-shot 示例的生成：
- `正式`、`书面语` → 正式书面语示例
- `会议`、`纪要` → 会议纪要格式示例
- `技术`、`文档` → 技术文档风格示例
- `数字`、`阿拉伯` → 数字转换示例
- `分段`、`换行` → 分段示例

### 热切换 Preset

右键托盘图标 → Preset → 选择，无需重启即可生效。

---

## 6. 热键配置

### 触发模式

| 模式 | 操作 | 适合场景 |
|------|------|---------|
| `ptt` | 按住录音，松开识别 | 快速短句输入 |
| `toggle` | 按一次开始，再按停止 | 长时间录音 |
| `oneshot` | 按一次录音固定时长 | 固定时长场景 |

### 热键格式

```json
{
  "hotkey": "<ctrl_r>",              // 右 Ctrl（PTT）
  "toggle_hotkey": "<ctrl>+<space>", // Ctrl+Space（Toggle）
  "exit_hotkey": "<ctrl>+<alt>+q",   // 退出
  "trigger_mode": "ptt"
}
```

常用热键格式：
- `<ctrl_r>`：右 Ctrl
- `<ctrl_l>`：左 Ctrl
- `<alt>+<space>`：Alt+Space
- `<ctrl>+<alt>+r`：Ctrl+Alt+R

### ASR Context（专业词汇）

提高专业术语识别准确率：

```json
{
  "asr_context": "Kubernetes, Docker, React, TypeScript, PostgreSQL"
}
```

---

## 7. 常见问题

### 模型加载失败

```bash
# 检查 CUDA 是否可用
python3 -c "import torch; print(torch.cuda.is_available())"

# 检查显存
nvidia-smi
```

显存不足时，改用 0.6B ASR 模型或 llama.cpp 精炼器。

### 热键不响应

1. 检查热键格式：`<ctrl_r>` 而不是 `Ctrl+R`
2. 确认没有其他程序占用该热键
3. 查看终端输出的错误信息

### 上屏失败

```bash
# Wayland 用户
sudo apt install wtype

# X11 用户
sudo apt install xdotool xsel

# 检查当前显示服务器
echo $XDG_SESSION_TYPE
```

### 托盘图标不显示

```bash
# Ubuntu/Debian
sudo apt install gir1.2-appindicator3-0.1

# Arch Linux
sudo pacman -S libappindicator-gtk3

# Fedora
sudo dnf install libappindicator-gtk3
```

### llama.cpp 编译失败

```bash
# 确认 CUDA toolkit 已安装
nvcc --version

# 指定 CUDA 路径
CMAKE_ARGS="-DLLAMA_CUDA=on -DCMAKE_CUDA_COMPILER=/usr/local/cuda/bin/nvcc" \
  pip install llama-cpp-python

# 或使用 CPU 版本（无 CUDA）
pip install llama-cpp-python
```

### 动画卡顿

1. 检查 GPU 驱动是否正确安装
2. 关闭其他占用 GPU 的程序
3. 确认 pyglet 版本兼容

### 卸载

```bash
./uninstall.sh
```
```

**Step 2: 确认内容完整**

```bash
grep "^## " docs/USER_GUIDE.md
```

Expected: 输出 7 个章节标题（## 1. 安装 到 ## 7. 常见问题）

**Step 3: Commit**

```bash
git add docs/USER_GUIDE.md
git commit -m "docs: 补充 USER_GUIDE.md（文本精炼器、Preset、热键、FAQ）"
```

---

### Task 4: 更新 README.md

**Files:**
- Modify: `README.md`

**Step 1: 用简化版本替换 README.md**

将 `README.md` 替换为以下内容：

```markdown
# Recordian

<div align="center">

<img src="assets/logo.png" width="150" height="150" alt="Recordian Logo"/>

### Linux 优先的智能语音输入助手

本地 ASR 识别 + AI 文本精炼 + 全局热键 + 实时动画

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux-orange.svg)](https://www.linux.org/)

</div>

---

## 核心功能

- **本地 ASR**：Qwen3-ASR 语音识别，中英混合，GPU 加速
- **AI 文本精炼**：去语气词、去重复、修标点，支持本地/云端/llama.cpp 三种后端
- **5 个内置 Preset**：日常、正式、总结、会议、技术，右键托盘一键切换
- **全局热键**：PTT / Toggle 两种模式，任意应用下触发
- **实时动画**：OpenGL 波形可视化，响应音量变化

## 安装

```bash
git clone https://github.com/zz8011/Recordian.git
cd Recordian
./install.sh
```

下载 ASR 模型（必需，约 3.5GB）：

```bash
pip install modelscope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir ./models/Qwen3-ASR-1.7B
```

## 使用

```bash
recordian-tray
```

按住**右 Ctrl** 录音，松开自动识别上屏。

## 文档

完整使用说明请查看 [docs/USER_GUIDE.md](docs/USER_GUIDE.md)，包含：
- 详细安装步骤和系统要求
- 配置文件说明
- 文本精炼器（本地 / llama.cpp / 云端）配置
- Preset 系统和自定义 Preset
- 热键配置
- 常见问题排查

## 许可证

MIT License - 详见 [LICENSE](LICENSE)
```

**Step 2: 确认 README 内容**

```bash
wc -l README.md
```

Expected: 行数在 50-70 之间

**Step 3: Commit**

```bash
git add README.md
git commit -m "docs: 简化 README，添加 USER_GUIDE 链接"
```

---

### Task 5: 最终验证

**Step 1: 确认所有链接有效**

```bash
# 确认 USER_GUIDE 存在
ls -la docs/USER_GUIDE.md

# 确认旧文件已删除
ls QUICKSTART.md INSTALL.md docs/llamacpp-guide.md docs/LLAMACPP_GUIDE.md 2>&1
```

Expected: USER_GUIDE.md 存在，旧文件均不存在

**Step 2: 确认 git 状态干净**

```bash
git status
```

Expected: `nothing to commit, working tree clean`

**Step 3: 推送到 GitHub**

```bash
git push origin master
```
