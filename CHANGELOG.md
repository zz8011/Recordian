# Changelog

## [0.1.0] - 2025-02-24

### Added
- 支持 llama.cpp (GGUF) 量化模型进行文本精炼
- 为 GGUF 模型实现 Few-shot prompt 机制
- **动态 Preset 系统**：可直接编辑 preset 文件改变行为
- 支持 4 种文本精炼 preset：
  - `default`: 去除重复词和语气助词，数字转阿拉伯数字，自动分段
  - `formal`: 转换为正式书面语
  - `technical`: 整理为技术文档风格
  - `meeting`: 整理为会议纪要格式
- 添加 LlamaCppTextRefiner 提供者
- 优化推理参数以提升输出质量
- 新增自定义 Preset 指南（`docs/CUSTOM_PRESET_GUIDE.md`）
- 新增示例 preset：`code-comment.md`

### Changed
- 优化 GGUF 模型的停止词设置
- 调整推理参数：temperature=0.1, repeat_penalty=1.2
- 改进 Few-shot prompt 示例以更好地引导模型
- **重构 Preset 系统**：从硬编码改为动态解析 preset 文件
- 支持通过关键词触发特定 Few-shot 示例：
  - "数字" 或 "阿拉伯" → 添加数字转换示例
  - "分段" 或 "换行" → 添加分段示例
  - "正式" 或 "书面语" → 使用正式书面语 Few-shot
  - "会议" 或 "纪要" → 使用会议纪要 Few-shot
  - "技术" 或 "文档" → 使用技术文档 Few-shot

### Fixed
- 修复 GGUF 模型输出 `<think>` 等无关内容的问题
- 修复 preset 的 prompt_template 覆盖 Few-shot prompt 的问题
- 修复输出被过早截断的问题

### Performance
- GGUF 模型显存占用降低至 ~600MB（vs transformers 的 ~2GB）
- 推理速度显著提升
- 模型文件大小仅 ~400MB（Qwen3-0.6B-Q4_K_M）

### Technical Details
- 使用 Few-shot prompt 替代 Chat Template
- 停止词：`["\n\n", "输入：", "<think>", "<|"]`
- 支持 GPU 加速（CUDA）和 CPU 后备
- 自动检测 preset 类型并应用对应的 Few-shot 示例
- **动态 Preset 解析**：
  - `_build_fewshot_prompt()` - 从 preset 文件动态构建 Few-shot
  - `_generate_fewshot_from_rules()` - 根据规则生成对应示例
  - `_build_default_fewshot()` - 灵活构建默认 Few-shot
- 支持用户自定义 preset，无需修改代码

## [0.0.1] - 2025-02-20

### Added
- 初始版本发布
- 支持 Qwen3-ASR 语音识别
- 支持 transformers 文本精炼
- 支持热键触发（右 Ctrl）
- 支持系统托盘图标
- 支持桌面通知
