# Preset 预设系统

## 功能说明

Preset 系统允许你创建和管理多个文本精炼 prompt 预设，可以随时切换不同的处理风格。

## 预设文件位置

所有预设文件存放在 `presets/` 目录下，使用 Markdown 格式（`.md`）。

## 内置预设

| 预设名称 | 文件 | 说明 |
|---------|------|------|
| `default` | `presets/default.md` | 默认：整理口语，去重去语气词 |
| `formal` | `presets/formal.md` | 正式书面语转换 |
| `summary` | `presets/summary.md` | 提取核心信息 |
| `meeting` | `presets/meeting.md` | 会议纪要格式 |
| `technical` | `presets/technical.md` | 技术文档风格 |

## 使用方法

### 方式 1：配置文件（推荐）

编辑 `~/.config/recordian/hotkey.json`：

```json
{
  "enable_text_refine": true,
  "refine_preset": "formal"
}
```

### 方式 2：命令行参数

```bash
uv run recordian-hotkey-dictate --enable-text-refine --refine-preset formal
```

### 方式 3：保存配置

```bash
uv run recordian-hotkey-dictate --enable-text-refine --refine-preset summary --save-config
```

## 切换预设

只需修改配置文件中的 `refine_preset` 值，重启程序即可：

```json
{
  "refine_preset": "default"    // 默认整理
  "refine_preset": "formal"     // 正式书面语
  "refine_preset": "summary"    // 简洁总结
  "refine_preset": "meeting"    // 会议纪要
  "refine_preset": "technical"  // 技术文档
}
```

## 创建自定义预设

### 1. 创建新文件

在 `presets/` 目录下创建新的 `.md` 文件，例如 `presets/my-style.md`：

```markdown
# 我的自定义风格

将以下文本转换为我喜欢的风格：
- 规则 1
- 规则 2
- 规则 3

原文：{text}

结果：
```

### 2. 使用自定义预设

```json
{
  "refine_preset": "my-style"
}
```

## 编辑预设

直接编辑 `presets/` 目录下的 `.md` 文件，保存后重启程序即可生效。

### 预设文件格式

```markdown
# 预设标题（可选，会被自动忽略）

你的 prompt 内容...
必须包含 {text} 占位符

原文：{text}

输出格式：
```

**注意**：
- 第一行如果是 `#` 标题会被自动忽略
- 必须包含 `{text}` 占位符
- 支持多行文本和格式化

## 测试预设

```bash
# 测试所有预设
./scripts/test_presets.py

# 测试特定预设
uv run python -c "
from recordian.preset_manager import PresetManager
mgr = PresetManager()
print(mgr.load_preset('formal'))
"
```

## 预设示例

### 示例 1：代码注释风格

`presets/code-comment.md`：
```markdown
# 代码注释风格

将以下口语转换为代码注释格式：
- 使用简洁的技术语言
- 突出关键逻辑和步骤
- 适合作为代码注释

原文：{text}

注释：
```

### 示例 2：邮件风格

`presets/email.md`：
```markdown
# 邮件风格

将以下口语整理为正式邮件内容：
- 使用礼貌用语
- 结构清晰（问候-正文-结尾）
- 保持专业得体

原文：{text}

邮件内容：
```

### 示例 3：笔记风格

`presets/note.md`：
```markdown
# 笔记风格

将以下内容整理为个人笔记：
- 使用要点列表
- 突出关键信息
- 简洁易读

原文：{text}

笔记：
```

## 优先级

如果同时设置了多个参数，优先级为：

1. `--refine-prompt`（命令行直接指定 prompt）
2. `--refine-preset`（使用预设文件）
3. `default` 预设（默认）

## 管理预设

```bash
# 列出所有预设
ls presets/*.md

# 查看预设内容
cat presets/formal.md

# 编辑预设
vim presets/formal.md

# 创建新预设
cp presets/default.md presets/my-preset.md
vim presets/my-preset.md
```

## 注意事项

- 预设文件必须是 UTF-8 编码
- 文件名不能包含空格（使用 `-` 或 `_`）
- 修改预设后需要重启程序
- 可以随时添加、删除、修改预设文件
- 建议备份自定义预设

## 测试效果

使用测试脚本查看不同预设的效果：

```bash
./scripts/test_presets.py
```

输出示例：
```
预设: formal
原文: 嗯那个我想说的就是这个这个项目呢需要添加一个新的功能
结果: 首先，该项目需要添加新的功能，并需去除重复内容。
```
