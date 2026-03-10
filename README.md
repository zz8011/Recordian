# Recordian

<div align="center">

<img src="assets/logo.png" width="150" height="150" alt="Recordian Logo"/>

### Linux 优先的智能语音输入助手

本地 ASR + 文本精炼 + 全局热键 + 可选语音唤醒

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux-orange.svg)](https://www.linux.org/)

</div>

## 项目简介

Recordian 面向 Linux 桌面语音输入场景，核心目标是把常用语音输入流程留在本地完成：

- 本地或 HTTP ASR 识别
- 可选二轮文本精炼
- 全局热键触发录音
- 托盘常驻与波形动画
- 可选语音唤醒、主人声纹校验
- 自动词库与自定义 Preset

## 当前特性

- **本地 ASR**：默认集成 `Qwen3-ASR` 流程，支持 GPU。
- **文本精炼**：支持 `local`、`cloud`、`llamacpp` 三种精炼后端。
- **Preset 系统**：内置核心预设 `default`、`formal`、`meeting`、`summary`、`technical`，并附带 `English`、`Japanese`、`Korean`、`Arabic`、`Extended` 示例预设。
- **智能输入方式**：支持 `auto` 与 `auto-fallback`，会自动检测窗口并选择合适的上屏方式。
- **自动检测 Electron 应用**：对微信、VS Code、Obsidian、Typora、Discord、Slack 等场景优先走更稳妥的粘贴路径。
- **语音唤醒**：支持“嗨/嘿 + 名字”唤醒，并可结合主人声纹验证。

## 安装

### 1. 安装系统依赖

`install.sh` 会检查这些依赖；也可以先手动安装：

```bash
sudo apt-get update
sudo apt-get install -y \
  python3-venv \
  python3-gi \
  gir1.2-appindicator3-0.1 \
  xdotool \
  xclip \
  libnotify-bin
```

如果你在 Wayland 下希望使用键盘模拟输入，再额外安装：

```bash
sudo apt-get install -y wtype
```

### 2. 克隆并安装

```bash
git clone https://github.com/zz8011/Recordian.git
cd Recordian
./install.sh
```

安装脚本会：

- 创建 `.venv`
- 安装 Python 依赖
- 创建桌面启动器
- 生成本地 `recordian-launch.sh`

### 3. 下载 ASR 模型

安装脚本默认**不会**自动拉取大模型。你有两个选择：

```bash
./install.sh --pull-external-model
```

或者手动下载：

```bash
source .venv/bin/activate
pip install modelscope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir ./models/Qwen3-ASR-1.7B
```

### 4. 手动安装方式

如果你不想使用安装脚本，也可以手动安装：

```bash
uv sync --extra gui --extra hotkey --extra qwen-asr --extra wake
```

或：

```bash
pip install -e ".[gui,hotkey,qwen-asr,wake]"
```

## 快速开始

启动托盘程序：

```bash
recordian-tray
```

默认交互：

- **右 Ctrl**：按住录音，松开识别并上屏
- **Ctrl+Alt+Q**：退出后台守护进程
- 托盘右键：打开设置、切换精炼预设、管理自动词库

首次启动时，即使还没有配置文件，也可以直接运行。配置保存后会写入：

```text
~/.config/recordian/hotkey.json
```

## 配置与文档

- 完整用户手册：[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)
- 快速参考：[`QUICK_REFERENCE.md`](QUICK_REFERENCE.md)
- 故障排查：[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)
- Preset 说明：[`presets/README.md`](presets/README.md)

常见配置项包括：

- `hotkey`
- `commit_backend`
- `qwen_model`
- `enable_text_refine`
- `refine_preset`
- `enable_auto_lexicon`
- `enable_voice_wake`

## 常用命令

```bash
# 启动托盘
recordian-tray

# 直接运行热键守护进程
recordian-hotkey-dictate

# 检查语音唤醒配置与模型状态
recordian-wake-diagnose
```

## 说明

- X11 环境下体验通常更完整；Wayland 建议准备 `wtype` 作为输入后端。
- 托盘菜单会自动读取 `presets/` 目录中的文本精炼预设文件；新增 `.md` 文件后可直接在菜单里看到。
- `auto-fallback` 会在主输入方式失败时按降级链继续尝试，适合复杂桌面环境。

## 许可证

MIT License，详见 [`LICENSE`](LICENSE)。
