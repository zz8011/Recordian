# Presets 目录

此目录包含文本精炼和 ASR Context 的 prompt 预设文件。

## 文本精炼预设（用于第二轮精炼）

- `default.md` - 默认：整理口语，去重去语气词
- `formal.md` - 正式书面语转换
- `summary.md` - 提取核心信息
- `meeting.md` - 会议纪要格式
- `technical.md` - 技术文档风格

### 使用方法

在配置文件中设置 `refine_preset` 参数：

```json
{
  "enable_text_refine": true,
  "refine_preset": "formal"
}
```

## ASR Context 预设（用于 ASR 识别阶段）

- `asr-default.md` - 默认：整理口语，去重去语气词
- `asr-simple.md` - 简洁模式：一句话指令
- `asr-formal.md` - 口语转书面语
- `asr-meeting.md` - 会议记录格式
- `asr-technical.md` - 技术文档风格

### 使用方法

在配置文件中设置 `asr_context_preset` 参数：

```json
{
  "asr_context_preset": "default"
}
```

或在托盘设置面板中选择 "ASR Context 预设"。

## 创建自定义预设

### 文本精炼预设

1. 在此目录创建新的 `.md` 文件
2. 编写 prompt，使用 `{text}` 作为占位符
3. 在配置中引用文件名（不含 `.md` 后缀）

### ASR Context 预设

1. 在此目录创建 `asr-{name}.md` 文件
2. 编写 prompt，**不需要** `{text}` 占位符
3. 在配置中设置 `asr_context_preset: "{name}"`

## 区别说明

- **文本精炼预设**：用于第二轮 LLM 精炼，需要 `{text}` 占位符
- **ASR Context 预设**：用于 ASR 识别阶段，作为 System Prompt，不需要占位符

详细文档：
- 文本精炼：`docs/text-refine.md`
- ASR Context：`docs/asr-context.md`
