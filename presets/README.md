# Presets 目录

此目录包含文本精炼 prompt 预设文件，并支持自定义 ASR Context 预设。

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

当前仓库默认不包含内置 `asr-*.md` 预设文件；如需使用 ASR Context 预设，请按下方“创建自定义预设”步骤自行添加。

### 使用方法

在配置文件中设置 `asr_context_preset` 参数：

```json
{
  "asr_context_preset": "meeting"
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
- 使用文档：`docs/USER_GUIDE.md`
- 故障排查：`docs/TROUBLESHOOTING.md`
