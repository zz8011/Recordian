# Recordian

<div align="center">

<img src="assets/logo.svg" width="150" height="150" alt="Recordian Logo"/>

### 🎙️ Linux 优先的智能语音输入助手

本地 ASR 识别 + AI 文本精炼 + 全局热键 + 炫酷动画

[![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Linux-orange.svg)](https://www.linux.org/)

[快速开始](#-快速开始) • [功能特性](#-核心功能) • [安装指南](#-安装) • [文档](#-文档)

</div>

---

## 💡 为什么选择 Recordian？

### 🔒 隐私至上，完全本地
- **零数据上传**：语音和文本完全保留在本地
- **离线运行**：无需网络连接即可使用
- **可选云端**：需要时可选择云端 API 增强

### ⚡ 简洁高效，开箱即用
- **一键启动**：托盘图标，随时可用
- **全局热键**：任意应用下都能触发
- **自动上屏**：识别完成自动输入到当前应用

### 🎨 现代设计，视觉反馈
- **实时动画**：音频波形可视化
- **状态提示**：录音、识别、处理状态一目了然
- **炫酷特效**：彩色椭圆动画，响应音量变化

---

## ✨ 核心功能

### 🎤 语音识别（ASR）

**支持的模型：**
- **Qwen3-ASR-1.7B**（推荐）：高准确率，中英混合，专业术语
- **Qwen3-ASR-0.6B**：更快速度，适合简单场景

**特点：**
- ✅ 完全本地离线运行
- ✅ 支持中英文混合识别
- ✅ 可提供专业词汇上下文
- ✅ GPU 加速，识别速度 ~1-2 秒
- ✅ 显存占用约 5.5GB（1.7B）

### 🤖 AI 文本精炼

**双引擎架构：**

| 引擎 | 模型 | 速度 | 质量 | 成本 | 隐私 |
|------|------|------|------|------|------|
| **本地** | Qwen3-0.6B | ⚡ 快（~3s） | ✅ 良好 | 💰 免费 | 🔒 完全本地 |
| **云端** | Claude 3.5 Sonnet | ⚠️ 中（~2-5s） | ⭐ 优秀 | 💳 按量付费 | ⚠️ 数据上传 |

**功能：**
- 去除语气词（嗯、啊、那个、这个）
- 去除重复词语和口误
- 修正标点符号
- 整理为通顺文本

**5 个内置预设：**
- `default` - 日常口语整理
- `formal` - 正式书面语
- `summary` - 简洁总结
- `meeting` - 会议纪要
- `technical` - 技术文档

### ⌨️ 热键系统

**三种触发模式：**

1. **PTT 模式**（按住说话）
   - 按住热键 → 开始录音
   - 松开热键 → 识别上屏
   - 适合：快速输入

2. **Toggle 模式**（一键开关）
   - 按一次 → 开始录音
   - 再按一次 → 停止识别
   - 适合：长时间录音

3. **混合模式**（同时支持）
   - 右 Ctrl（按住）→ PTT 快速输入
   - 右 Ctrl + Space → Toggle 长录音
   - 灵活切换，满足不同场景

**默认热键：**
- **录音触发**：右 Ctrl（PTT）
- **Toggle 开关**：右 Ctrl + Space
- **退出程序**：Ctrl + Alt + Q

### 🎨 系统托盘 GUI

**功能：**
- 📊 实时音频波形动画
- 🎨 彩色椭圆层，响应音量变化
- 🔔 状态图标（空闲/录音/处理/错误）
- ⚙️ 右键菜单快速切换预设
- 📈 性能统计显示

**动画效果：**
- 4 色椭圆层（蓝、紫、绿、红）
- 音量越大，椭圆旋转越快
- 球体投影变形，3D 视觉效果
- 柔和发光，现代设计风格

### 🖥️ 系统集成

- **自动上屏**：支持 Wayland (wtype) / X11 (xdotool)
- **剪贴板优先**：CJK 字符友好，避免逐字输入
- **桌面通知**：录音和识别状态实时提示
- **终端检测**：自动切换为 Ctrl+Shift+V 粘贴

---

## 🚀 快速开始

### 📦 安装

#### 方式 1：一键安装（推荐）

```bash
# 克隆仓库
git clone https://github.com/zz8011/Recordian.git
cd Recordian

# 运行安装脚本
./install.sh
```

安装完成后：
- 从应用菜单搜索 "Recordian" 启动
- 或双击 `recordian-launch.sh` 快速启动

#### 方式 2：手动安装

```bash
# 创建虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 安装依赖
pip install -e .[qwen-asr,hotkey,gui]
```

### 📥 下载模型

```bash
# 安装 modelscope
pip install modelscope

# 下载 ASR 模型（必需，约 3.5GB）
modelscope download --model Qwen/Qwen3-ASR-1.7B --local_dir ./models/Qwen3-ASR-1.7B

# 下载文本精炼模型（可选，用于本地精炼，约 1.2GB）
modelscope download --model Qwen/Qwen3-0.6B --local_dir ./models/Qwen3-0.6B
```

### 🎯 启动使用

```bash
# 启动托盘 GUI（推荐）
recordian-tray

# 或使用命令行模式
recordian-hotkey-dictate \
  --asr-provider qwen-asr \
  --qwen-model ./models/Qwen3-ASR-1.7B \
  --enable-text-refine \
  --refine-model ./models/Qwen3-0.6B \
  --save-config
```

**使用步骤：**
1. 按住 **右 Ctrl** 开始录音（看到动画）
2. 说话（椭圆随音量旋转）
3. 松开热键，自动识别并上屏
4. 按 **Ctrl+Alt+Q** 退出程序

---

## 📖 配置说明

### 配置文件位置

配置文件：`~/.config/recordian/hotkey.json`

### 推荐配置

#### 日常使用（本地模型，快速免费）

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

#### 重要场合（云端 API，高质量）

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

### 托盘菜单快速切换

右键托盘图标 → Preset → 选择预设：
- Default（日常）
- Formal（正式）
- Summary（总结）
- Meeting（会议）
- Technical（技术）

---

## 🎨 使用场景

### 场景 1：日常聊天（快速）

**配置：** 本地引擎 + default 预设

**效果：**
```
输入：嗯，那个，我觉得这个这个这个方案是，呃，比较好的。
输出：我觉得这个方案是比较好的。
```

### 场景 2：会议纪要（专业）

**配置：** 云端引擎 + meeting 预设

**效果：**
```
输入：然后呢，我们讨论了一下，嗯，关于那个项目进度的问题，大家都觉得，呃，应该加快速度...
输出：会议讨论了项目进度问题，一致认为应当加快推进速度。
```

### 场景 3：技术文档（准确）

**配置：** 云端引擎 + technical 预设 + ASR context

```json
{
  "refine_preset": "technical",
  "asr_context": "Kubernetes, Docker, React, TypeScript, PostgreSQL"
}
```

**效果：**
```
输入：我们使用库伯内特斯部署多克容器，前端用瑞克特和泰普斯克瑞普特...
输出：我们使用 Kubernetes 部署 Docker 容器，前端使用 React 和 TypeScript。
```

---

## 📊 性能数据

### ASR 模型对比

| 模型 | 简单场景 | 专业术语 | 中英混合 | 速度 | 显存 |
|------|---------|---------|---------|------|------|
| **Qwen3-ASR-1.7B** | ✅ 优秀 | ✅ 优秀 | ✅ 优秀 | ~1.2s | ~5.5GB |
| Qwen3-ASR-0.6B | ✅ 良好 | ⚠️ 一般 | ⚠️ 一般 | ~0.8s | ~3.5GB |

**推荐：** 使用 1.7B 模型，质量更好，速度差异不大

### 文本精炼对比

| 引擎 | 速度 | 质量 | 成本 | 隐私 | 推荐场景 |
|------|------|------|------|------|---------|
| **本地 (0.6B)** | ~3s | 良好 | 免费 | 完全本地 | 日常聊天、快速输入 |
| 云端 (Claude) | ~2-5s | 优秀 | 按量付费 | 数据上传 | 会议纪要、正式文档 |

**推荐策略：**
- 日常使用：本地引擎（快速、免费、隐私）
- 重要场合：云端引擎（质量更高）
- 托盘菜单快速切换

---

## 🔧 高级功能

### 自定义预设

在 `presets/` 目录下创建 `.md` 文件：

```markdown
---
name: 我的自定义预设
description: 适用于代码注释整理
---

请将以下口语化文本整理为简洁的代码注释：
- 去除语气词和重复
- 使用技术术语
- 保持简洁明了

原文：{text}

整理后：
```

使用：
```json
{"refine_preset": "my-custom"}
```

### ASR Context（专业词汇）

提高专业术语识别准确率：

```json
{
  "asr_context": "Recordian, Qwen3-ASR, Kubernetes, Docker, React, TypeScript, PostgreSQL, Redis"
}
```

### 热键配置

```json
{
  "hotkey": "<ctrl_r>",           // PTT 热键
  "toggle_hotkey": "<ctrl>+<space>",  // Toggle 热键
  "exit_hotkey": "<ctrl>+<alt>+q",    // 退出热键
  "trigger_mode": "ptt"           // ptt / toggle / oneshot
}
```

### 环境变量

```bash
# 设备选择
export RECORDIAN_DEVICE=cuda  # cuda / cpu / auto

# 调试模式
export RECORDIAN_DEBUG=1

# 剪贴板超时（毫秒）
export RECORDIAN_CLIPBOARD_TIMEOUT_MS=500

# 粘贴快捷键覆盖
export RECORDIAN_PASTE_SHORTCUT="ctrl+v"
```

---

## 📁 项目结构

```
Recordian/
├── src/recordian/              # 源代码
│   ├── tray_gui.py             # 托盘 GUI + 动画
│   ├── hotkey_dictate.py       # 热键守护进程
│   ├── linux_commit.py         # 文本上屏
│   ├── linux_notify.py         # 桌面通知
│   ├── engine.py               # 识别引擎
│   ├── config.py               # 配置管理
│   ├── preset_manager.py       # 预设管理
│   └── providers/              # ASR 和 LLM 提供商
│       ├── qwen_asr.py         # Qwen3-ASR
│       ├── qwen_text_refiner.py # 本地文本精炼
│       └── cloud_llm_refiner.py # 云端文本精炼
├── assets/                     # 资源文件
│   ├── logo.svg                # 托盘图标（空闲）
│   ├── logo-recording.svg      # 托盘图标（录音）
│   ├── logo-error.svg          # 托盘图标（错误）
│   └── logo-warming.svg        # 托盘图标（预热）
├── presets/                    # 预设文件
│   ├── default.md              # 日常预设
│   ├── formal.md               # 正式预设
│   ├── summary.md              # 总结预设
│   ├── meeting.md              # 会议预设
│   └── technical.md            # 技术预设
├── docs/                       # 文档
│   ├── logo-guide.md           # 图标设计指南
│   ├── tray-guide.md           # 托盘使用指南
│   ├── presets.md              # 预设系统说明
│   ├── text-refine.md          # 文本精炼说明
│   └── quick-switch.md         # 快速切换指南
├── tests/                      # 测试文件
├── models/                     # 模型文件（需下载）
├── install.sh                  # 安装脚本
├── uninstall.sh                # 卸载脚本
├── make-release.sh             # 打包脚本
├── INSTALL.md                  # 安装指南
├── CLAUDE.md                   # 项目说明（开发用）
└── README.md                   # 本文件
```

---

## 📚 文档

- **安装指南**：[INSTALL.md](INSTALL.md)
- **托盘使用**：[docs/tray-guide.md](docs/tray-guide.md)
- **预设系统**：[docs/presets.md](docs/presets.md)
- **文本精炼**：[docs/text-refine.md](docs/text-refine.md)
- **快速切换**：[docs/quick-switch.md](docs/quick-switch.md)
- **图标设计**：[docs/logo-guide.md](docs/logo-guide.md)

---

## 🔍 故障排查

### 模型加载失败

```bash
# 检查 CUDA 是否可用
python -c "import torch; print(torch.cuda.is_available())"

# 检查显存
nvidia-smi

# 如果显存不足，使用 0.6B 模型
modelscope download --model Qwen/Qwen3-ASR-0.6B --local_dir ./models/Qwen3-ASR-0.6B
```

### 热键不响应

1. 检查热键格式：`<ctrl_r>` 而不是 `Ctrl+R`
2. 确认没有其他程序占用该热键
3. 尝试使用其他热键：`<ctrl>+<alt>+r`
4. 查看终端输出的错误信息

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

### 动画卡顿

1. 检查 GPU 驱动是否正确安装
2. 降低动画复杂度（修改 `tray_gui.py` 中的参数）
3. 关闭其他占用 GPU 的程序

---

## 🎯 路线图

### 已完成 ✅
- [x] Qwen3-ASR 本地识别
- [x] 双引擎文本精炼（本地 + 云端）
- [x] 预设系统（5 个内置 + 自定义）
- [x] 热键系统（PTT + Toggle + 混合）
- [x] 托盘 GUI + 实时动画
- [x] 配置文件管理
- [x] 桌面启动器
- [x] 一键安装脚本

### 计划中 📋
- [ ] 流式 ASR（边录边识别）
- [ ] Fcitx5/IBus 输入法引擎集成
- [ ] 更多 LLM Provider 支持（OpenAI、Gemini）
- [ ] 语音命令系统
- [ ] 多语言支持（英语、日语）
- [ ] 历史记录管理
- [ ] 云端配置同步

---

## 🛠️ 系统要求

### 最低要求
- **操作系统**：Linux (X11 或 Wayland)
- **Python**：3.10+
- **显存**：4GB+（使用 0.6B 模型）
- **内存**：8GB+

### 推荐配置
- **操作系统**：Ubuntu 22.04+ / Arch Linux
- **Python**：3.11+
- **GPU**：NVIDIA GPU（6GB+ 显存）
- **显存**：8GB+（使用 1.7B 模型）
- **内存**：16GB+

### 依赖软件
- **Wayland**：wtype
- **X11**：xdotool, xsel/xclip
- **托盘**：libappindicator-gtk3
- **通知**：libnotify

---

## 🤝 贡献

欢迎贡献代码、报告问题、提出建议！

### 如何贡献

1. Fork 本仓库
2. 创建特性分支：`git checkout -b feature/amazing-feature`
3. 提交更改：`git commit -m 'Add amazing feature'`
4. 推送到分支：`git push origin feature/amazing-feature`
5. 提交 Pull Request

### 报告问题

请在 [Issues](https://github.com/zz8011/Recordian/issues) 页面报告问题，包含：
- 操作系统和版本
- Python 版本
- 错误信息和日志
- 复现步骤

---

## 📄 许可证

本项目采用 MIT 许可证 - 详见 [LICENSE](LICENSE) 文件

---

## 🙏 致谢

- [Qwen3-ASR](https://github.com/QwenLM/Qwen-Audio) - 优秀的语音识别模型
- [Qwen3](https://github.com/QwenLM/Qwen) - 强大的语言模型
- [pynput](https://github.com/moses-palmer/pynput) - 全局热键支持
- [pystray](https://github.com/moses-palmer/pystray) - 系统托盘支持
- [pyglet](https://github.com/pyglet/pyglet) - OpenGL 动画渲染

---

<div align="center">

### Made with ❤️ for Linux users

**Star ⭐ 本项目以支持开发！**

[报告问题](https://github.com/zz8011/Recordian/issues) • [功能建议](https://github.com/zz8011/Recordian/issues) • [讨论交流](https://github.com/zz8011/Recordian/discussions)

</div>
