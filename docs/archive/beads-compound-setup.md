# beads-compound 安装完成

## 安装日期
2026-03-02

## 安装内容

### 全局安装 (~/.claude)

#### 命令 (27个)
- **工作流**: /beads-plan, /beads-brainstorm, /beads-work, /beads-parallel, /beads-review, /beads-compound, /beads-checkpoint
- **规划**: /beads-deepen, /beads-plan-review, /beads-triage
- **工具**: /lfg, /changelog, /create-agent-skill, /generate-command, /heal-skill
- **测试**: /test-browser, /xcode-test, /reproduce-bug, /report-bug
- **文档**: /deploy-docs, /release-docs, /feature-video, /agent-native-audit
- **并行**: /resolve-pr-parallel, /resolve-todo-parallel

#### 代理 (28个)
- 审查代理
- 研究代理
- 设计代理
- 工作流代理
- 文档代理

#### 技能 (15个)
- git-worktree
- brainstorming
- create-agent-skills
- agent-native-architecture
- beads-knowledge
- agent-browser
- andrew-kane-gem-writer
- dhh-rails-style
- dspy-ruby
- every-style-editor
- file-todos
- frontend-design
- gemini-imagegen
- rclone
- skill-creator

#### MCP 服务器
- Context7 (框架文档查询)

### 项目级安装 (Recordian)

#### 内存系统
- **位置**: `.beads/memory/`
- **知识库**: `knowledge.jsonl`
- **召回脚本**: `recall.sh`
- **知识数据库**: `knowledge-db.sh`

#### Hooks
1. **SessionStart**: `auto-recall.sh` (异步)
   - 会话开始时自动召回相关知识
   - 基于当前 beads 任务

2. **PostToolUse**: `memory-capture.sh` (异步)
   - 捕获 `bd comment` 中的知识
   - 支持标签: LEARNED, DECISION, FACT, PATTERN, INVESTIGATION

3. **SubagentStop**: `subagent-wrapup.sh`
   - 子代理停止时的清理工作

## 使用方法

### 1. 基本工作流

```bash
# 规划功能
/beads-plan Fix OAuth login redirect

# 头脑风暴
/beads-brainstorm 如何优化音频处理性能

# 执行工作
/beads-work <bead-id>

# 代码审查
/beads-review

# 并行工作
/beads-parallel
```

### 2. 知识管理

#### 记录知识
```bash
# 使用 bd comment 记录学习
bd comment add <bead-id> "LEARNED: OAuth redirect_uri 必须完全匹配，包括尾部斜杠"
bd comment add <bead-id> "DECISION: 使用 Sherpa-ONNX 作为默认 ASR 引擎"
bd comment add <bead-id> "FACT: PyAudio 在 Ubuntu 需要 portaudio19-dev"
bd comment add <bead-id> "PATTERN: 所有音频流使用上下文管理器确保清理"
```

#### 召回知识
```bash
# 手动搜索知识
.beads/memory/recall.sh "OAuth"
.beads/memory/recall.sh "音频"

# 自动召回（会话开始时）
# 系统会自动根据当前 beads 召回相关知识
```

### 3. 高级功能

#### 深化规划
```bash
/beads-deepen <bead-id>
```

#### 检查点
```bash
/beads-checkpoint
```

#### 召回知识（会话中）
```bash
/beads-recall <关键词>
```

## 平台支持

beads-compound 支持以下平台：
- ✅ Claude Code (已安装)
- ✅ OpenCode
- ✅ Gemini CLI

## 文件结构

```
Recordian/
├── .beads/
│   └── memory/
│       ├── knowledge.jsonl      # 知识库
│       ├── recall.sh            # 召回脚本
│       └── knowledge-db.sh      # 数据库管理
├── .claude/
│   ├── hooks/
│   │   ├── auto-recall.sh       # 自动召回
│   │   ├── memory-capture.sh    # 内存捕获
│   │   ├── subagent-wrapup.sh   # 子代理清理
│   │   ├── knowledge-db.sh      # 知识数据库
│   │   └── provision-memory.sh  # 内存配置
│   └── settings.json            # Hook 配置
└── .gitignore                   # 已更新

~/.claude/
├── commands/                    # 27 个命令
├── agents/                      # 28 个代理
├── skills/                      # 15 个技能
├── hooks/                       # 全局 hooks
├── .mcp.json                    # MCP 配置
└── settings.json                # 全局设置
```

## 注意事项

1. **重启 Claude Code**
   - 安装完成后需要重启 Claude Code 才能加载插件

2. **知识捕获**
   - 只有使用特定标签的 `bd comment` 才会被捕获
   - 标签: LEARNED, DECISION, FACT, PATTERN, INVESTIGATION

3. **自动召回**
   - 每次会话开始时自动运行
   - 基于当前打开的 beads 任务

4. **全局 vs 项目级**
   - 命令、代理、技能：全局可用
   - 内存系统：项目级（需要 .beads 目录）

## 下一步

1. 重启 Claude Code
2. 尝试使用 `/beads-plan` 创建任务
3. 使用 `bd comment add` 记录知识
4. 下次会话时查看自动召回效果

## 相关链接

- [beads-compound GitHub](https://github.com/roberto-mello/beads-compound-plugin)
- [Beads 项目](https://github.com/steveyegge/beads)
- [安装位置](~/beads-compound-plugin)

---

**安装完成时间**: 2026-03-02 04:22
