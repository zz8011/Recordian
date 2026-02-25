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
- **AI 文本精炼**：去语气词、去重复、修标点，支持本地 / llama.cpp / 云端三种后端
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
