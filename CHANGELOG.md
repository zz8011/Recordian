# 更新日志

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 的组织方式，版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

只有打上 `v*` 标签的版本才会通过 `release.yml` 发布到 PyPI。目前仅 `v0.1.0` 有标签，`0.1.1` 与 `0.1.2` 是 `pyproject.toml` 中的开发版本号，尚未发布。

## [Unreleased]

### 新增

- Rust ABI1 运行时的离线 release 构建与原子安装脚本，附库 SHA256、编译器信息及相对路径的 Rust 源码哈希元数据。
- `RECORDIAN_BUILD_NATIVE=1` 可选打包构建；包含 `.so` 的 wheel 使用 Linux 平台标签，sdist 包含 Rust 源码与构建脚本并排除原型和编译产物。
- 原生 CI 覆盖 Cargo test/Clippy、构建失败与 ABI 拒绝行为、分发产物检查及 required-native Python 回归；新增中文构建、部署和回滚指南。

### 说明

- 默认自动选择可用原生库，缺库时使用 Python；部署使用 `RECORDIAN_NATIVE_CORE=required`，回滚显式选择 `python`。不在音频回调中构建。
- Rust 范围是量化、RMS、PCM 有界 FIFO 与持久 GIO 调用；Python UI、Agent、纠词及 C++/CUDA 模型保留，推理调度保留 160 ms，拒绝未通过延迟门槛的 320 ms 候选。
- 2026-10-09 的生产调用点 CPU 转换/RMS 微基准与 Python / required-native 全回归结果见 Rust 指南；合并前真实模型候选、GPU、输入窗口与自然麦克风验收尚待完成。

## [0.1.2] - 2026-10-07 · 未发布

### 新增

- 设置窗口「文字润色 → 云端 HTTP」支持从可编辑的 API 地址与 Key 中识别模型：点击「识别模型」后读取 OpenAI 兼容的 `/v1/models` 列表，结果填入可手动编辑的下拉框。识别不自动选择、不自动保存，失败时仍可手动填写模型标识。
- API Key 以草稿方式编辑：留空保留已保存值，保存后输入框清空。

### 变更

- 上下文纠错的默认 provider 由 `semif` 改为 `jev`（本机 Plumb 服务的官方 CLI）。已有配置中的 `semif` 仍然有效。
- `--semif-endpoint` 现在只服务于 `semif` provider；`jev` 在留空时回退到 `~/.hermes/jev/plumb.json`。

### 修复

- 清理原生托盘导入，修复 CI 中的相关报错。
- 优化 jev 纠错管线，补充日志与监控。

## [0.1.1] - 2026-10-07 · 未发布

### 新增

- 接入定稿的 Recordian 品牌资产：应用图标、托盘状态图标与设置窗口标记。
- 托盘六类状态图形（空闲 / 启动 / 录音 / 处理 / 错误 / 停止），未知状态回退到默认图标。

### 变更

- 托盘使用包内透明 PNG，路径缓存只在图标组变化时更新，不引入新的动画定时器。
- README 与 `INDEX.md` 同步更新品牌信息。

### 说明

- ASR、录音和模型实现沿用 0.1.0，本版本只涉及桌面呈现。
- 彩色托盘图标面向深色托盘，暂未自动切换浅色主题资源。

## [0.1.0] - 2026-02-24

首个发布版本（PyPI 标签 `v0.1.0`）。

### 新增

- 动态 Preset 系统：直接编辑 preset 文件即可改变行为。
- Preset 热切换：托盘菜单一键切换，无需重启后端。
- llama.cpp (GGUF) 支持：显存占用降低约 70%，速度提升约 30%。
- Few-shot Prompt 机制，解决 GGUF 模型输出不稳定问题。
- 增强的 `default` preset：支持阿拉伯数字与自动分段。
- 关键词触发特定行为（数字、分段、正式、会议、技术）。

### 技术改进

- 所有 refiner 支持 `update_preset()` 方法。
- 每次录音时自动检查配置变化并热更新。
- 智能规则检测，自动生成 Few-shot prompt。

[Unreleased]: https://github.com/zz8011/Recordian/compare/v0.1.0...HEAD
[0.1.2]: https://github.com/zz8011/Recordian/compare/v0.1.0...HEAD
[0.1.1]: https://github.com/zz8011/Recordian/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zz8011/Recordian/releases/tag/v0.1.0

<!-- 0.1.1 与 0.1.2 尚无 tag，两条链接暂时指向同一范围；打 tag 后应改为逐版本对比。 -->
