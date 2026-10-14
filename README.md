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

## 核心功能

- **本地 ASR**：默认集成 `Qwen3-ASR` 流程，支持 GPU；也可接入 HTTP 云服务和本机 Confucius4-R2T2 流式服务。
- **文本精炼**：支持 `local`、`cloud`、`llamacpp` 三种精炼后端。
- **Preset 系统**：内置 14 个文本精炼预设，新增 `.md` 文件即可在托盘菜单里直接看到。
- **智能输入方式**：支持 `auto` 与 `auto-fallback`，自动检测窗口并选择合适的上屏方式。
- **自动检测 Electron 应用**：对微信、VS Code、Obsidian、Typora、Discord、Slack 等场景优先走更稳妥的粘贴路径。
- **连续听写**：一次触发持续识别，按段提交；配合 Fcitx 插件可做到预编辑与最终提交分离。
- **语音唤醒**：支持「嗨/嘿 + 称呼」唤醒，并可结合主人声纹验证。

## 设置窗口

从托盘菜单打开「设置」。窗口沿用 GTK 3，以 Omarchy Labra 配色、细边框和侧栏导航呈现十个页面：

| 页面 | 当前可编辑范围 |
| --- | --- |
| 偏好设置 | 开机自启，以及常用设置入口 |
| 日常听写 | 触发方式、麦克风与语言下拉、流式上屏、自动回车 |
| 文字与输入 | 输入后端、多行常用词与显式替换；独立工具管理自动词库 |
| 识别服务 | Confucius / Qwen / HTTP 来源、对应服务地址或本机模型与设备、超时 |
| 文字润色 | 启用开关、统一 API 地址和 Key、模型识别与可输入的下拉选择 |
| 快捷键 | PTT、开关和结束键、触发间隔 |
| 语音唤醒与声纹 | 唤醒前缀、称呼、静音结束时间、声纹验证开关及同风格注册向导 |
| Agent | 功能与唤醒入口开关、独立实例配置、本机程序发现；支持 Hermes 和 Claude Code |
| 录音参数 | 采集后端、采样率、声道、单次录音时长 |
| 运行与诊断 | 预热、诊断、润色样本开关和配置检查 |

修改先留在窗口草稿，点击「保存设置」才写入配置；保存只合并修改过的字段，保留未更改的密钥和配对信息。「恢复默认」只修改表单显示字段，仍需保存才生效。开机自启只改变下次登录行为，不启动或停止当前进程；未检测到可配置的桌面服务时该开关不可用。

麦克风刷新只枚举设备，不打开麦克风。枚举在后台运行，同时最多一个任务；刷新期间按钮显示「正在刷新…」，后端切换合并为最新请求。取消修改或恢复默认会同步恢复对应后端的选项，暂时断开的已配置设备仍显示并保留，不会被自动换成其他设备。

本机和远端润色使用同一 API 配置入口；旧的进程内模型配置保留，只有明确点击迁移才改用 API。模型识别只读取模型列表，不执行推理、下载模型或验证所选模型的润色能力。远程粘贴暂不显示，其旧配置保留。Agent 发现只检查可执行文件路径，尚无执行适配的 CLI 置灰，不会启动它们。

GTK 3 是当前设置窗口的界面依赖，不表示功能缺失或需要迁移到 GTK 4。测试环境中 Python 3.14 / PyGObject 的 asyncio 弃用提示来自兼容层，与偏好页开关无关；开关以现有配置和服务能力为准。声纹注册需用户明确开始，VAD 和模型路径等未显示参数保留在配置文件中。

本轮设备刷新修复、隔离 GTK 验证、可重复资源测量与未测范围见 [设置设备刷新验证](docs/SETTINGS-DEVICE-REFRESH.zh-CN.md)。

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

安装脚本会创建 `.venv`、安装 Python 依赖、创建桌面启动器，并生成本地 `recordian-launch.sh`。

### 3. 下载 ASR 模型

安装脚本默认**不会**自动拉取大模型。你有两个选择：

```bash
./install.sh --pull-external-model
```

或者手动下载：

```bash
source .venv/bin/activate
pip install modelscope
modelscope download --model Qwen/Qwen3-ASR-0.6B --local_dir ./models/Qwen3-ASR-0.6B
```

### 4. 手动安装方式

如果你不想使用安装脚本：

```bash
uv sync --extra gui --extra hotkey --extra qwen-asr --extra wake
```

或：

```bash
pip install -e ".[gui,hotkey,qwen-asr,wake]"
```

可用的 extras：`gui`、`hotkey`、`qwen-asr`、`confucius-asr`、`correction`、`wake`、`dev`。

### 5. 可选 Rust 运行时

Linux 上可在安装前运行 `python scripts/build_native_core.py`，或在构建 wheel 时设置
`RECORDIAN_BUILD_NATIVE=1`。共享库负责音频量化、RMS、有界 PCM FIFO 与持久 GIO/Fcitx 调用；
UI、Agent、纠词和 C++/CUDA 模型仍沿用现有实现，推理调度保留 160 ms。
未安装库时默认使用 Python；原生部署应显式设置 `RECORDIAN_NATIVE_CORE=required`，
回滚使用 `RECORDIAN_NATIVE_CORE=python`。构建不会发生在录音回调中。
系统依赖、平台 wheel、双 Python 环境部署和待完成的真实模型验收见
[`Rust 运行时指南`](docs/RUST-RUNTIME.zh-CN.md)。

## 快速开始

启动托盘程序：

```bash
recordian-tray
```

默认交互（本机推荐配置，可从托盘「设置 → 快捷键」修改）：

- **右 Ctrl**：按住录音，松开识别并上屏
- **右 Alt**：点一下开始连续听写，再点一下结束（需要支持分段提交的 Fcitx 插件）
- **Ctrl+Alt+Q**：退出后台守护进程
- 托盘右键：打开设置、切换精炼预设、管理自动词库

首次启动时，即使还没有配置文件，也可以直接运行。配置保存后会写入：

```text
~/.config/recordian/hotkey.json
```

## 命令

| 命令 | 作用 |
| --- | --- |
| `recordian-tray` | 托盘 GUI，日常入口 |
| `recordian-hotkey-dictate` | 热键守护进程，用 `--config-path` 指定 JSON 配置 |
| `recordian` | 单次 WAV 文件识别（`--wav`），不读 `--config-path` |
| `recordian-linux-dictate` | 命令行听写：按固定时长录音（`--duration`）后识别并提交，可指定输入设备与 ASR 后端 |
| `recordian-agent` | 本地 Agent 任务面板 |
| `recordian-remote-paste-agent` | 远程粘贴 Agent |
| `recordian-wake-diagnose` | 检查语音唤醒配置与模型状态 |
| `recordian-vllm-realtime-probe` | 探测本机 vLLM `/v1/realtime` 是否支持边发边出字 |

常用调用：

```bash
# 用已保存的设置启动热键守护进程
recordian-hotkey-dictate --config-path "$HOME/.config/recordian/hotkey.json"

# 本机 Confucius4-R2T2 流式 ASR 服务（完整参数见 server/README-confucius.md）
python server/confucius_streaming_server.py --model-dir /path/to/Confucius4-R2T2 --r2t2-source /path/to/checkout

# 检查语音唤醒配置与模型状态
recordian-wake-diagnose

# 用 WAV 探测本机 vLLM /v1/realtime 是否真的支持边发边出字
recordian-vllm-realtime-probe --wav /path/to/sample.wav --model Qwen3-ASR-0.6B --url http://127.0.0.1:8000
```

如果 vLLM 实例支持 realtime，`recordian-vllm-realtime-probe` 会把增量转写直接打印到标准输出；如果握手阶段就返回 `HTTP 403`，通常表示当前实例没有真正启用 `/v1/realtime`，或者所加载模型不支持 realtime。

## 主要配置项

配置文件为 `~/.config/recordian/hotkey.json`。下表中的「默认」指程序内置默认值；热键类项的默认值来自本机推荐配置（`recommended_profile.py`）：

| 配置项 | 说明 |
| --- | --- |
| `hotkey` | PTT 触发键，默认 `<ctrl_r>` |
| `toggle_hotkey` | 连续听写开关键，推荐配置 `<alt_r>` |
| `exit_hotkey` | 退出守护进程，推荐配置 `<ctrl>+<alt>+q` |
| `asr_provider` | `qwen-asr`（默认）/ `http-cloud` / `confucius-asr` |
| `commit_backend` | `auto`（默认）/ `auto-fallback` / `fcitx` / `wtype` / `xdotool` / `xdotool-clipboard` / `stdout` / `none` |
| `enable_text_refine` | 是否启用二轮文本精炼 |
| `refine_provider` | `local`（默认）/ `cloud` / `llamacpp` |
| `refine_preset` | 使用的精炼预设名（不含 `.md`），默认 `default` |
| `enable_auto_lexicon` | 自动词库开关 |
| `enable_voice_wake` | 语音唤醒开关 |

## 文档

完整文档索引见 [`docs/README.md`](docs/README.md)。常用入口：

- 用户手册：[`docs/USER_GUIDE.md`](docs/USER_GUIDE.md)
- 快速参考：[`docs/QUICK-REFERENCE.md`](docs/QUICK-REFERENCE.md)
- 故障排查：[`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md)
- Preset 说明：[`presets/README.md`](presets/README.md)
- 版本历史：[`CHANGELOG.md`](CHANGELOG.md)
- 项目结构与模块导航：[`INDEX.md`](INDEX.md)

专项文档：

- Rust 运行时构建、打包、部署与回滚：[`docs/RUST-RUNTIME.zh-CN.md`](docs/RUST-RUNTIME.zh-CN.md)（生产性能与自然麦克风验收仍待完成）
- 本地 vLLM/OpenAI 兼容 ASR 配置示例：[`examples/hotkey.http-cloud.local-vllm.json`](examples/hotkey.http-cloud.local-vllm.json)
- 本机 Confucius4-R2T2 流式 ASR：服务与实测限制见 [`server/README-confucius.md`](server/README-confucius.md)，配置示例 [`examples/hotkey.confucius-asr.local.json`](examples/hotkey.confucius-asr.local.json)（PTT：`Ctrl_R` 按住录音、松开结束；服务端单会话音频上限默认 30 秒，超限显式报错而非截断）
- 流式输入法方案设计与验收：[`docs/STREAMING-IME-PLAN.zh-CN.md`](docs/STREAMING-IME-PLAN.zh-CN.md)；实测验证记录：[`docs/STREAMING-IME-VALIDATION.zh-CN.md`](docs/STREAMING-IME-VALIDATION.zh-CN.md)
- 连续听写、语境纠词和口述数字网址：[`docs/CONTINUOUS-DICTATION.zh-CN.md`](docs/CONTINUOUS-DICTATION.zh-CN.md)
- 热词纠错策略：[`docs/HOTWORD-POLICY.md`](docs/HOTWORD-POLICY.md)
- Fcitx 输入法插件（预编辑/提交）：构建与安装见 [`fcitx/recordian-commit/README.md`](fcitx/recordian-commit/README.md) 与 `fcitx/recordian-commit/build.sh`；改动后需按该文档重载 Fcitx 生效

## 说明

- X11 环境下体验通常更完整；Wayland 建议准备 `wtype` 作为输入后端。
- 托盘菜单会自动读取 `presets/` 目录中的文本精炼预设文件；新增 `.md` 文件后可直接在菜单里看到。
- `auto-fallback` 会在主输入方式失败时按降级链继续尝试，适合复杂桌面环境。

## 许可证

MIT License，详见 [`LICENSE`](LICENSE)。
