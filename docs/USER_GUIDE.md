# Recordian 用户手册

## 1. 项目定位

Recordian 是一个面向 Linux 桌面的语音输入工具，核心链路是：

1. 录音
2. ASR 识别
3. 可选文本精炼
4. 将结果提交到当前窗口

典型使用方式是常驻托盘运行，通过全局热键或语音唤醒触发录音。

## 2. 系统要求

- Linux 桌面环境
- Python 3.10+
- 建议准备可用的麦克风输入设备
- 如果要使用托盘 GUI，需要 `python3-gi` 和 `AppIndicator3`
- 如果要在 X11 中自动上屏，建议安装 `xdotool`、`xclip`、`xprop`
- 如果在 Wayland 中自动上屏，建议安装 `wtype`

推荐安装命令：

```bash
sudo apt-get update
sudo apt-get install -y \
  python3-venv \
  python3-gi \
  gir1.2-appindicator3-0.1 \
  xdotool \
  xclip \
  x11-utils \
  libnotify-bin
```

Wayland 可选：

```bash
sudo apt-get install -y wtype
```

## 3. 安装

### 3.1 使用安装脚本

```bash
git clone https://github.com/zz8011/Recordian.git
cd Recordian
./install.sh
```

安装脚本会自动：

- 创建 `.venv`
- 安装 `gui`、`hotkey`、`qwen-asr`、`wake` 相关依赖
- 创建桌面启动器
- 生成本地启动脚本 `recordian-launch.sh`

### 3.2 下载模型

默认安装流程不会下载体积较大的 ASR 模型。你可以：

```bash
./install.sh --pull-external-model
```

或者手动下载：

```bash
source .venv/bin/activate
pip install modelscope
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir ./models/Qwen3-ASR-1.7B
```

### 3.3 手动安装

```bash
uv sync --extra gui --extra hotkey --extra qwen-asr --extra wake
```

或：

```bash
pip install -e ".[gui,hotkey,qwen-asr,wake]"
```

## 4. 快速开始

启动托盘：

```bash
recordian-tray
```

默认行为：

- `右 Ctrl`：按住录音，松开识别并上屏
- `Ctrl+Alt+Q`：退出守护进程
- 托盘右键：打开设置、切换文本精炼预设、管理常用词和自动词库

如果你希望先只生成默认运行配置，也可以执行：

```bash
recordian-hotkey-dictate --save-config
```

配置文件默认位置：

- 主运行配置：`~/.config/recordian/hotkey.json`
- 自动词库数据库：`~/.config/recordian/auto_lexicon.db`
- 主人声纹画像：`~/.config/recordian/owner_voice_profile.json`
- 参考示例：`examples/hotkey.http-cloud.local-vllm.json`

## 5. 关键配置

一个常见的 `hotkey.json` 可以像这样：

```json
{
  "hotkey": "<ctrl_r>",
  "exit_hotkey": "<ctrl>+<alt>+q",
  "trigger_mode": "ptt",
  "input_device": "default",
  "record_backend": "auto",
  "commit_backend": "auto",
  "asr_provider": "qwen-asr",
  "qwen_model": "./models/Qwen3-ASR-1.7B",
  "qwen_language": "Chinese",
  "enable_text_refine": true,
  "refine_provider": "local",
  "refine_preset": "default",
  "enable_auto_lexicon": true,
  "auto_lexicon_db": "~/.config/recordian/auto_lexicon.db",
  "enable_voice_wake": false,
  "wake_prefix": ["嗨", "嘿"],
  "wake_name": ["小二"]
}
```

重点字段说明：

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `hotkey` | `<ctrl_r>` | 按住说话模式的触发热键 |
| `trigger_mode` | `ptt` | `ptt` / `toggle` / `oneshot` |
| `commit_backend` | `auto` | 上屏策略，推荐 `auto` 或 `auto-fallback` |
| `asr_provider` | `qwen-asr` | 当前支持 `qwen-asr` 与 `http-cloud` |
| `qwen_model` | 空字符串 | Qwen3-ASR 模型路径；通常指向本地模型目录 |
| `enable_text_refine` | `false` | 是否启用二轮文本精炼 |
| `refine_provider` | `local` | `local` / `cloud` / `llamacpp` |
| `refine_preset` | `default` | 文本精炼预设名 |
| `enable_auto_lexicon` | `true` | 是否开启自动词库学习 |
| `enable_voice_wake` | `false` | 是否开启语音唤醒 |

## 6. 文本精炼与 Preset

仓库内置 5 个常用文本精炼预设：

- `default`
- `formal`
- `meeting`
- `summary`
- `technical`

仓库还附带一组示例预设：

- `English`
- `Japanese`
- `Korean`
- `Arabic`
- `Extended`

你也可以直接在 `presets/` 目录中新增 `.md` 文件。托盘菜单会自动发现这些文件，并把它们加入“切换预设”菜单。

文本精炼预设支持在首行使用可选的后处理指令，例如：

```text
@postprocess: zh-stutter-lite
```

当前支持：

- `none`
- `repeat-lite`
- `zh-stutter-lite`

更多说明见 [`presets/README.md`](../presets/README.md)。

## 7. 常用词与自动词库

Recordian 有两层词增强机制：

1. 手动维护的 `asr_context`
2. 自动词库 `auto_lexicon.db`

自动词库会从成功上屏的文本里学习高频词，并在后续识别时回灌为热词。对于人名、术语、项目名，这通常比单纯改提示词更稳定。

托盘菜单中可以直接进行：

- 常用词编辑
- 自动词库数据库导出
- 自动词库数据库导入

## 8. 智能输入方式

`commit_backend` 决定识别结果如何提交到当前窗口。

推荐值：

- `auto`：默认策略，自动选择最合适的提交方式
- `auto-fallback`：自动选择 + 降级机制，失败时继续尝试备用方案

可选值：

- `xdotool-clipboard`
- `xdotool`
- `wtype`
- `stdout`
- `none`

### 自动检测 Electron 应用

Recordian 会优先识别特定桌面窗口类型，再决定是直接打字还是走剪贴板粘贴。

支持的 Electron 应用包括：

- 微信
- VS Code
- Obsidian
- Typora
- Discord
- Slack

对于这些应用，剪贴板粘贴通常比逐字输入更稳定，尤其是中文或混合文本场景。

### 降级机制

当你使用 `auto-fallback` 时，Recordian 会在主提交方式失败后继续尝试备用方案。

当前降级链为：

1. `xdotool-clipboard`
2. `xdotool`
3. `wtype`
4. `stdout`

这也是文档和代码中提到的 `auto-fallback` 行为。

## 9. 语音唤醒

启用 `enable_voice_wake` 后，后台会进入低功耗监听模式。常用配置包括：

- `wake_prefix`
- `wake_name`
- `wake_auto_stop_silence_s`
- `wake_no_speech_timeout_s`
- `wake_owner_verify`
- `wake_owner_profile`
- `wake_owner_sample`

诊断命令：

```bash
recordian-wake-diagnose
```

这个命令会检查：

- 配置文件是否启用语音唤醒
- 关键词与模型文件是否存在
- 主人声纹相关文件是否齐全

## 10. 常用命令

```bash
# 托盘模式
recordian-tray

# 仅运行热键守护进程
recordian-hotkey-dictate

# 音频文件转写
recordian --mode utterance --wav sample.wav --pass1 http --pass1-endpoint http://127.0.0.1:8000/v1/audio/transcriptions

# 语音唤醒诊断
recordian-wake-diagnose
```

## 11. 常见问题

### 11.1 启动后没有配置文件

这是正常的。`recordian-tray` 可以直接以默认值运行；只有当你保存设置或执行 `--save-config` 时，配置才会写入磁盘。

### 11.2 默认热键是什么

默认是 `右 Ctrl`，不是 `Ctrl+Alt+V`。

### 11.3 Wayland 下为什么上屏不稳定

Wayland 对窗口控制更严格，建议安装 `wtype`，并优先使用 `auto-fallback` 让系统按降级链自动尝试。

### 11.4 如何确认当前窗口为什么没有正常上屏

先检查：

```bash
which xdotool
which xprop
echo $XDG_SESSION_TYPE
```

如果是 X11 环境但缺少 `xprop`，窗口类型探测会退化；这会影响自动判断 Electron 应用与终端窗口的能力。

## 12. 故障排查

详细问题请查看：

- [`docs/TROUBLESHOOTING.md`](TROUBLESHOOTING.md)
- [`QUICK_REFERENCE.md`](../QUICK_REFERENCE.md)

如果重点在“为什么某个桌面应用没有正确上屏”，优先看故障排查文档中与 `xprop`、窗口识别、自动检测机制、Electron 应用、降级机制相关的部分。
