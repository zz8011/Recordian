# Agent Instructions

代码导航使用 codegraph，见下文「代码导航：codegraph」。

## Project Navigation — Read INDEX.md First

> **进门先读 `INDEX.md`**。这是项目的导航地图，列出了文件夹结构、关键文件位置、起点文件。
> 读 INDEX.md 后才知道「去哪找」，而不是上来就 grep + 读源码蛮力搜。

关键索引文件：
- `INDEX.md` — 项目主索引（src/recordian/ 各模块一览）
- `src/recordian/providers/INDEX.md` — ASR provider 和 text refiner 模块索引

## Non-Interactive Shell Commands

**ALWAYS use non-interactive flags** with file operations to avoid hanging on confirmation prompts.

Shell commands like `cp`, `mv`, and `rm` may be aliased to include `-i` (interactive) mode on some systems, causing the agent to hang indefinitely waiting for y/n input.

**Use these forms instead:**
```bash
# Force overwrite without prompting
cp -f source dest           # NOT: cp source dest
mv -f source dest           # NOT: mv source dest
rm -f file                  # NOT: rm file

# For recursive operations
rm -rf directory            # NOT: rm -r directory
cp -rf source dest          # NOT: cp -r source dest
```

**Other commands that may prompt:**
- `scp` - use `-o BatchMode=yes` for non-interactive
- `ssh` - use `-o BatchMode=yes` to fail instead of prompting
- `apt-get` - use `-y` flag
- `brew` - use `HOMEBREW_NO_AUTO_UPDATE=1` env var

## 代码导航：codegraph

查找符号、调用关系和改动影响范围时，优先使用 codegraph 的 `codegraph_explore`，比逐个 grep 和读文件更省上下文。

- 本仓库已建立索引（`.codegraph/`，由 `codegraph init` 生成）。

## Landing the Plane (Session Completion)

**When ending a work session**, you MUST complete ALL steps below. Work is NOT complete until `git push` succeeds.

**MANDATORY WORKFLOW:**

1. **List remaining work** - Put anything that needs follow-up in the final message
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **PUSH TO REMOTE** - This is MANDATORY:
   ```bash
   git pull --rebase
   git push
   git status  # MUST show "up to date with origin"
   ```
4. **Clean up** - Clear stashes, prune remote branches
5. **Verify** - All changes committed AND pushed
6. **Hand off** - Provide context for next session

**CRITICAL RULES:**
- Work is NOT complete until `git push` succeeds
- NEVER stop before pushing - that leaves work stranded locally
- NEVER say "ready to push when you are" - YOU must push
- If push fails, resolve and retry until it succeeds
