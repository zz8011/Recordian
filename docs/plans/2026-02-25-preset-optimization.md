# Preset 优化实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 参考 default preset 的详细规则体系，优化其他 5 个 preset 文件，提升输出质量和一致性

**Architecture:** 为每个 preset 添加 6 大类规则（去除语气词、标点符号、数字判断、场景特定规则、分段格式、输出要求），保持各 preset 特色定位，总长度约 20-25 行

**Tech Stack:** Markdown 文件编辑

---

## Task 1: 优化 formal.md

**Files:**
- Modify: `presets/formal.md`

**Step 1: 备份当前版本**

Run: `cp presets/formal.md presets/formal.md.bak`
Expected: 备份文件创建成功

**Step 2: 更新 formal.md 内容**

将文件内容替换为：

```markdown
# 正式书面语预设

请将以下口语转换为正式书面语，严格遵循以下规则：

1. **去除口语化表达**：
   - 删除所有语气词：嗯、啊、呃、哦、那个、这个、然后、就是等
   - 删除犹豫和重复词
   - 将口语词汇替换为书面语词汇

2. **标点符号使用规则**：
   - 疑问句使用问号（？）
   - 陈述句使用句号（。）
   - 根据语气准确使用逗号、顿号、分号等

3. **阿拉伯数字判断规则**：
   - 数据、统计、编号使用阿拉伯数字
   - 正式场合的重要数字可使用中文大写
   - 根据上下文智能判断

4. **书面语转换规则**：
   - 使用正式的书面语词汇和句式
   - 避免缩略语和网络用语
   - 保持专业、得体的语气

5. **分段和格式**：
   - 根据逻辑关系合理分段
   - 保持段落间的连贯性

6. **输出要求**：
   - 只输出转换后的正式文本
   - 不要添加任何解释或说明

原文：{text}
```

**Step 3: 验证文件格式**

Run: `cat presets/formal.md | head -5`
Expected: 显示文件前 5 行，确认格式正确

**Step 4: 提交更改**

```bash
git add presets/formal.md
git commit -m "refactor(preset): 优化 formal preset 规则详细程度

参考 default preset，添加 6 大类详细规则：
- 去除口语化表达规则
- 标点符号使用规范
- 数字判断规则
- 书面语转换规则
- 分段格式规则
- 输出要求

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 2: 优化 technical.md

**Files:**
- Modify: `presets/technical.md`

**Step 1: 备份当前版本**

Run: `cp presets/technical.md presets/technical.md.bak`
Expected: 备份文件创建成功

**Step 2: 更新 technical.md 内容**

将文件内容替换为：

```markdown
# 技术文档预设

请将以下口语整理为技术文档风格，严格遵循以下规则：

1. **去除语气词和重复词**：
   - 删除所有语气词和犹豫词
   - 删除重复表达

2. **标点符号使用规则**：
   - 疑问句使用问号（？）
   - 陈述句使用句号（。）
   - 列表项使用分号或句号

3. **阿拉伯数字和技术符号**：
   - 版本号、参数、数据一律使用阿拉伯数字
   - 保留技术符号和代码片段的原始格式
   - API、URL、命令等保持原样

4. **技术文档风格规则**：
   - 使用准确的技术术语，避免口语化表达
   - 结构清晰，逻辑严谨
   - 突出关键步骤和注意事项
   - 必要时使用列表或分段提升可读性

5. **分段和格式**：
   - 按照技术流程或逻辑层次分段
   - 重要概念或步骤可单独成段

6. **输出要求**：
   - 只输出整理后的技术文档内容
   - 不要添加任何解释或元信息

原文：{text}
```

**Step 3: 验证文件格式**

Run: `cat presets/technical.md | head -5`
Expected: 显示文件前 5 行，确认格式正确

**Step 4: 提交更改**

```bash
git add presets/technical.md
git commit -m "refactor(preset): 优化 technical preset 规则详细程度

参考 default preset，添加 6 大类详细规则：
- 去除语气词规则
- 标点符号规范
- 数字和技术符号规则
- 技术文档风格规则
- 分段格式规则
- 输出要求

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 3: 优化 meeting.md

**Files:**
- Modify: `presets/meeting.md`

**Step 1: 备份当前版本**

Run: `cp presets/meeting.md presets/meeting.md.bak`
Expected: 备份文件创建成功

**Step 2: 更新 meeting.md 内容**

将文件内容替换为：

```markdown
# 会议纪要预设

请将以下口语整理为会议纪要格式，严格遵循以下规则：

1. **去除语气词和重复词**：
   - 删除所有语气词、犹豫词和重复表达
   - 保留关键信息和决策内容

2. **标点符号使用规则**：
   - 使用句号、逗号等标准标点
   - 列表项使用分号或句号
   - 保持格式统一

3. **阿拉伯数字判断规则**：
   - 时间、日期、数量使用阿拉伯数字
   - 编号和序号使用阿拉伯数字

4. **会议纪要格式规则**：
   - 使用条目列表或分段结构
   - 突出关键决策、行动项和责任人
   - 保持客观、专业的记录风格
   - 按照讨论顺序或主题分类组织内容

5. **要点提炼**：
   - 提取核心讨论内容和结论
   - 明确标注待办事项和截止时间
   - 保留重要的背景信息

6. **输出要求**：
   - 只输出整理后的会议纪要
   - 不要添加任何解释或说明

原文：{text}
```

**Step 3: 验证文件格式**

Run: `cat presets/meeting.md | head -5`
Expected: 显示文件前 5 行，确认格式正确

**Step 4: 提交更改**

```bash
git add presets/meeting.md
git commit -m "refactor(preset): 优化 meeting preset 规则详细程度

参考 default preset，添加 6 大类详细规则：
- 去除语气词规则
- 标点符号规范
- 数字判断规则
- 会议纪要格式规则
- 要点提炼规则
- 输出要求

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 4: 优化 summary.md

**Files:**
- Modify: `presets/summary.md`

**Step 1: 备份当前版本**

Run: `cp presets/summary.md presets/summary.md.bak`
Expected: 备份文件创建成功

**Step 2: 更新 summary.md 内容**

将文件内容替换为：

```markdown
# 简洁总结预设

请提取以下文本的核心信息，严格遵循以下规则：

1. **去除冗余内容**：
   - 删除所有语气词、重复词和无关细节
   - 只保留核心观点和关键信息

2. **标点符号使用规则**：
   - 使用简洁明了的标点
   - 多个要点可使用顿号或分号连接

3. **阿拉伯数字判断规则**：
   - 关键数据使用阿拉伯数字
   - 保持数字表达的准确性

4. **总结提炼规则**：
   - 高度浓缩，用最少的文字表达核心内容
   - 如果是单一主题，用一句话概括
   - 如果是多个要点，用简短列表呈现
   - 保持信息的完整性和准确性

5. **输出格式**：
   - 简短文本直接输出一句话
   - 较长文本可分点列出（2-5个要点）
   - 避免冗长的句子和段落

6. **输出要求**：
   - 只输出提炼后的核心信息
   - 不要添加"总结："等前缀或说明

原文：{text}
```

**Step 3: 验证文件格式**

Run: `cat presets/summary.md | head -5`
Expected: 显示文件前 5 行，确认格式正确

**Step 4: 提交更改**

```bash
git add presets/summary.md
git commit -m "refactor(preset): 优化 summary preset 规则详细程度

参考 default preset，添加 6 大类详细规则：
- 去除冗余内容规则
- 标点符号规范
- 数字判断规则
- 总结提炼规则
- 输出格式规则
- 输出要求

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 5: 优化 code-comment.md

**Files:**
- Modify: `presets/code-comment.md`

**Step 1: 备份当前版本**

Run: `cp presets/code-comment.md presets/code-comment.md.bak`
Expected: 备份文件创建成功

**Step 2: 更新 code-comment.md 内容**

将文件内容替换为：

```markdown
# 代码注释预设

请将以下口语整理为代码注释风格，严格遵循以下规则：

1. **去除语气词和重复词**：
   - 删除所有语气词和口语化表达
   - 删除重复和冗余内容

2. **标点符号使用规则**：
   - 简短注释可省略句号
   - 多句注释使用标准标点
   - 保持注释风格的一致性

3. **阿拉伯数字规则**：
   - 所有数字一律使用阿拉伯数字
   - 参数、索引、计数等使用数字表示

4. **代码注释风格规则**：
   - 使用简洁明了的技术语言
   - 使用准确的技术术语和专业词汇
   - 突出关键逻辑、算法思路和注意事项
   - 适合直接作为代码注释使用

5. **简洁性要求**：
   - 尽可能精简，避免冗长描述
   - 一句话能说清楚就不用两句
   - 去除不必要的修饰词

6. **输出要求**：
   - 只输出整理后的注释内容
   - 不要添加注释符号（如 //、#）
   - 不要添加任何解释或说明

原文：{text}
```

**Step 3: 验证文件格式**

Run: `cat presets/code-comment.md | head -5`
Expected: 显示文件前 5 行，确认格式正确

**Step 4: 提交更改**

```bash
git add presets/code-comment.md
git commit -m "refactor(preset): 优化 code-comment preset 规则详细程度

参考 default preset，添加 6 大类详细规则：
- 去除语气词规则
- 标点符号规范
- 数字规则
- 代码注释风格规则
- 简洁性要求
- 输出要求

Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
```

---

## Task 6: 清理备份文件

**Files:**
- Delete: `presets/*.md.bak`

**Step 1: 删除所有备份文件**

Run: `rm -f presets/*.md.bak`
Expected: 备份文件被删除

**Step 2: 验证删除**

Run: `ls presets/*.bak 2>/dev/null || echo "备份文件已清理"`
Expected: 显示 "备份文件已清理"

---

## Task 7: 测试验证（可选）

**Files:**
- Read: `tests/test_preset_manager.py`
- Run: `scripts/test_presets.py` (如果存在)

**Step 1: 检查是否有测试脚本**

Run: `test -f scripts/test_presets.py && echo "存在" || echo "不存在"`
Expected: 显示测试脚本是否存在

**Step 2: 如果存在，运行测试**

Run: `python scripts/test_presets.py` (如果存在)
Expected: 所有 preset 加载成功，格式正确

**Step 3: 手动验证 preset 加载**

Run:
```bash
python -c "
from recordian.preset_manager import PresetManager
mgr = PresetManager()
for name in ['formal', 'technical', 'meeting', 'summary', 'code-comment']:
    content = mgr.load_preset(name)
    print(f'{name}: {len(content)} 字符')
"
```
Expected: 每个 preset 都能成功加载，字符数明显增加

---

## 完成标准

- [ ] 所有 5 个 preset 文件已更新
- [ ] 每个 preset 包含 6 大类规则
- [ ] 每个 preset 约 20-25 行
- [ ] 所有更改已提交到 git
- [ ] preset 文件可以正常加载
- [ ] 备份文件已清理
