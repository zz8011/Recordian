# Recordian 文档索引

本页是文档的导航入口。项目根目录的 [`README.md`](../README.md) 讲「这是什么、怎么装」，本页帮你找到具体主题的深入说明。

## 入门

| 文档 | 内容 |
| --- | --- |
| [`USER_GUIDE.md`](USER_GUIDE.md) | 用户手册：系统要求、安装、关键配置、文本精炼、常用词、智能输入方式、语音唤醒、常见问题 |
| [`QUICK-REFERENCE.md`](QUICK-REFERENCE.md) | 一页速查：启动命令、默认热键、配置文件位置、常见配置片段 |
| [`DESKTOP-QUICKSTART.zh-CN.md`](DESKTOP-QUICKSTART.zh-CN.md) | 本机已配置好的 Confucius 流式识别 + Fcitx 桥接的日常用法 |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | 故障排查：动画无响应、录音无声音、识别不准、热键不响应、文本上屏失败 |
| [`VENV_SETUP.md`](VENV_SETUP.md) | 虚拟环境里 PyGObject (`gi`) 的配置说明 |

## 功能专题

| 文档 | 内容 |
| --- | --- |
| [`CONTINUOUS-DICTATION.zh-CN.md`](CONTINUOUS-DICTATION.zh-CN.md) | 连续听写、语境纠词、口述数字与网址 |
| [`CONTINUOUS-DICTATION-VALIDATION.zh-CN.md`](CONTINUOUS-DICTATION-VALIDATION.zh-CN.md) | 上述功能的验收记录（2026-09-25） |
| [`HOTWORD-POLICY.md`](HOTWORD-POLICY.md) | 热词纠错策略：哪些能确定性修正、哪些必须交给判断器 |
| [`VOICE-WAKE.zh-CN.md`](VOICE-WAKE.zh-CN.md) | 语音唤醒的本机配置与验证（Sherpa ONNX 中文 INT8 模型） |
| [`AGENT-VOICE.zh-CN.md`](AGENT-VOICE.zh-CN.md) | Agent 语音入口：Recordian 负责录音与识别，Hermes 负责执行任务 |
| [`SYSTEM-VOICE-INPUT.zh-CN.md`](SYSTEM-VOICE-INPUT.zh-CN.md) | 系统默认语音输入与模型运行方案（GGUF Q4_K_M + Q8 音频编码器） |

## 流式输入法

| 文档 | 内容 |
| --- | --- |
| [`STREAMING-IME-PLAN.zh-CN.md`](STREAMING-IME-PLAN.zh-CN.md) | 本地流式语音输入方案的设计、依据与验收标准 |
| [`STREAMING-IME-VALIDATION.zh-CN.md`](STREAMING-IME-VALIDATION.zh-CN.md) | 实测验证记录（2026-09-24），含可复查的运行证据 |

## 本机环境

| 文档 | 内容 |
| --- | --- |
| [`OMARCHY-SETUP.zh-CN.md`](OMARCHY-SETUP.zh-CN.md) | Omarchy 桌面环境的适配与安装说明 |

## 开发

| 文档 | 内容 |
| --- | --- |
| [`API.md`](API.md) | 核心模块的接口说明（配置管理、识别与精炼管线） |
| [`RUST-RUNTIME.zh-CN.md`](RUST-RUNTIME.zh-CN.md) | Rust ABI1、离线构建、平台 wheel、双 Python 环境部署、显式回滚、已测 CPU/全回归与待完成的模型验收 |
| [`../INDEX.md`](../INDEX.md) | 项目结构、模块地图与「改哪里」导航 |
| [`../CHANGELOG.md`](../CHANGELOG.md) | 版本更新日志 |

## 归档

[`archive/`](archive/README.md) 存放不再维护的历史文档：过程记录（Sprint 交付、提交清单快照）、一次性技术研究（语音唤醒 CPU 优化、ONNX 量化）、历史设计稿，以及未发布的 Wiki 页面。其中的路径和默认值可能已经过时。
