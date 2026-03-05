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

## 项目初衷

在 Linux 开发场景里，像 Speakless 这样体验成熟的语音输入工具并不常见，这会明显拉低 vibe coding 的效率。  
Recordian 因此而生：它把语音识别能力尽可能留在本地，依托用户自己的模型与算力，解决日常开发中的语音输入问题，减少键盘负担，提升创作和编码节奏。

因为我自己比较“懒”，也为了更自然地进入工作流，我加入了语音唤醒能力。这样即使不按任何热键，也能在离线环境下持续办公，用语音直接和模型交互，让思路表达更连贯、操作更轻松。  
希望这套工具也能对你有帮助。

## 核心功能

- **本地 ASR**：Qwen3-ASR 语音识别，中英混合，GPU 加速
- **AI 文本精炼**：语义优先去语气词/重复词，修标点；支持本地 / llama.cpp / 云端三种后端
- **Preset 可控后处理**：在 `presets/*.md` 顶部使用 `@postprocess`（`none` / `repeat-lite` / `zh-stutter-lite`）
- **个人词库联动保护词**：二轮精炼会从自动词库热词中提取关键词，提示模型"原样保留"
- **5 个内置 Preset**：日常、正式、总结、会议、技术，右键托盘一键切换
- **常用词 + 自动词库**：手动维护常用词，自动学习高频词并注入热词
- **词库数据库工具**：常用词管理窗口支持自动词库数据库导入/导出
- **智能输入方式**：自动检测 Electron 应用（微信、VS Code、Obsidian 等），选择最佳输入方式
- **全局热键**：PTT / Toggle 两种模式，任意应用下触发
- **语音唤醒**：可选常驻监听，支持"嗨/嘿 + 名字"唤醒，自动静音结束
- **主人声纹门控**：支持"仅主人声音可唤醒"，并可在会话内辅助过滤非主人语音
- **实时动画**：OpenGL 波形可视化，响应音量变化

## 安装

```bash
git clone https://github.com/zz8011/Recordian.git
cd Recordian
./install.sh
```

安装脚本默认**不拉取外部模型**。如果你希望安装时顺带拉取外部 ASR 模型，可选：

```bash
./install.sh --pull-external-model
```

也可自定义模型 ID / 下载目录：

```bash
./install.sh --pull-external-model \
  --external-model-id Qwen/Qwen3-ASR-1.7B \
  --external-model-dir ./models/Qwen3-ASR-1.7B
```

或手动安装（不使用安装脚本）：

```bash
uv sync --extra gui --extra hotkey --extra qwen-asr --extra wake
# 或
pip install -e ".[gui,hotkey,qwen-asr,wake]"
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
- 常用词管理、自动词库、数据库导入/导出
- 文本精炼器（本地 / llama.cpp / 云端）配置
- Preset 系统和自定义 Preset
- 热键配置
- 常见问题排查

## 许可证

MIT License - 详见 [LICENSE](LICENSE)
