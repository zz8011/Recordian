# Recordian

Linux 优先的语音输入助手，本地 ASR + 全局热键 + 系统托盘。

## 命令

```bash
# 安装（开发模式）
pip install -e .[dev]

# 安装本地 FunASR
pip install -e .[funasr]

# 安装 Qwen3-ASR（GPU 推理）
pip install -e .[qwen-asr]

# 安装热键支持
pip install -e .[hotkey]

# 安装 GUI 托盘
pip install -e .[gui]

# 运行测试
pytest

# 启动热键守护进程（读取 ~/.config/recordian/hotkey.json）
source .venv/bin/activate
recordian-hotkey-dictate
```

## 架构

```
src/recordian/
├── models.py          # 核心数据结构（ASRResult, CommitResult, SessionState 等）
├── config.py          # Pass2PolicyConfig, AppConfig
├── audio.py           # 音频读写与分块工具
├── policy.py          # Pass2 触发策略（置信度/英文比例/热词）
├── engine.py          # DictationEngine（单通道 pass1+pass2）
├── realtime.py        # RealtimeDictationEngine（流式 pass1+pass2）
├── cli.py             # recordian 命令行入口
├── linux_commit.py    # 文本上屏后端（xdotool-clipboard/wtype/pynput/stdout）
├── linux_dictate.py   # 单次听写流程
├── hotkey_dictate.py  # 全局热键守护进程（PTT + toggle 双模式）
├── linux_notify.py    # 桌面通知（notify-send/stdout）
├── tray_gui.py        # 系统托盘 + 波纹动画（pyglet + pystray）
├── benchmark.py       # CER/延迟/RTF 评估工具
├── runtime_deps.py    # ffmpeg 运行时依赖管理
└── providers/
    ├── base.py             # ASRProvider 抽象基类
    ├── streaming_base.py   # StreamingASRProvider 抽象基类
    ├── funasr_local.py     # FunASR 本地整句识别
    ├── funasr_streaming.py # FunASR 流式识别（Paraformer）
    ├── qwen_asr.py         # Qwen3-ASR 本地识别（transformers 后端）
    └── http_cloud.py       # 通用 HTTP 云端 ASR
```

## 入口点

| 命令 | 模块 | 用途 |
|------|------|------|
| `recordian` | `cli:main` | 通用 CLI（utterance/realtime-sim 模式） |
| `recordian-linux-dictate` | `linux_dictate:main` | 单次麦克风听写 |
| `recordian-hotkey-dictate` | `hotkey_dictate:main` | 全局热键守护进程 |
| `recordian-tray` | `tray_gui:main` | 托盘 GUI |

## 热键模式

`hotkey_dictate` 支持三种触发模式（`--trigger-mode`）：

| 模式 | 行为 |
|------|------|
| `ptt` | 按住热键录音，松开停止上屏 |
| `toggle` | 按一下开始，再按停止上屏 |
| `oneshot` | 按一下，录制固定时长后自动上屏 |

**PTT + toggle 并行**（ptt 模式下）：同时配置 `--toggle-hotkey` 和 `--stop-hotkey`，两套模式共用同一个录音引擎，互不干扰。

当前默认配置（`~/.config/recordian/hotkey.json`）：
- 右 Ctrl 按住 → PTT
- 右 Ctrl + Space → toggle 开始
- 右 Ctrl（toggle 录音中）→ toggle 停止
- Ctrl + Alt + Q → 退出守护进程

## ASR Provider

| Provider | 参数 | 说明 |
|----------|------|------|
| `funasr` | `--model` | FunASR 本地整句/流式识别，默认 pass1 |
| `qwen-asr` | `--qwen-model` | Qwen3-ASR transformers 后端，GPU 推理，带标点 |

Qwen3-ASR 相关参数：
- `--qwen-model`：模型路径或名称（优先于 `--model`）
- `--qwen-language`：语言提示，默认 `Chinese`，`auto` 自动检测
- `--qwen-max-new-tokens`：生成 token 上限，默认 `1024`

## 上屏策略

`linux_commit.py` 优先剪贴板粘贴，不逐字打字：
- X11 + xdotool + xsel/xclip → `xdotool-clipboard`（默认，CJK 友好）
- Wayland → `wtype`
- 终端窗口自动切换为 `Ctrl+Shift+V`
- 可用 `RECORDIAN_PASTE_SHORTCUT` 手动覆盖

## 环境变量

| 变量 | 说明 |
|------|------|
| `RECORDIAN_HOTKEY` | 触发热键（默认 `<ctrl_r>`） |
| `RECORDIAN_EXIT_HOTKEY` | 退出热键 |
| `RECORDIAN_DEVICE` | `cpu/cuda/auto`（默认 `auto`） |
| `RECORDIAN_CONFIG_PATH` | 配置文件路径 |
| `RECORDIAN_PASTE_SHORTCUT` | 手动覆盖粘贴快捷键 |
| `RECORDIAN_CLIPBOARD_TIMEOUT_MS` | 剪贴板自动清空超时（毫秒），0 表示禁用 |
| `RECORDIAN_DEBUG=1` | 开启诊断日志 |

## 测试

```bash
pytest                          # 全量测试
pytest tests/test_policy.py     # 单文件
pytest -k "commit"              # 按关键字过滤
```

测试均为纯 mock，无需真实模型或麦克风。

## 当前里程碑

- M1 ✅：一键启动、PTT、托盘、动画、GPU warmup、基本识别
- M2 ✅：上屏稳定化（xdotool-clipboard）、焦点修复、Qwen3-ASR 集成、PTT+toggle 并行
- M3 📋：IBus/Fcitx5 引擎级接入（真正输入法协议 commit）
