# S: Recordian Tray GUI UX/Perf 修复规格

> 项目: Recordian (`/tmp/Recordian`)
> 目标: 修复 tray_gui.py 中审查出的 12 个问题
> 分支: refactor/deep-optimization
> 范围: 仅修改 `src/recordian/tray_gui.py` + 新增测试

## 需求清单

### P0 — 性能 & 稳定性

#### R1: 缓存 ConfigManager.load 调用 (#12)
- **现状**: `_update_tray_menu()` → `_gtk_update()` 每次 event 都 `ConfigManager.load()` 读磁盘
- **问题**: streaming 模式下 stream_partial 每秒多次 event，造成大量磁盘 I/O
- **修复**: 引入实例级 `_cached_config: dict | None` + `_config_mtime: float`，`ConfigManager.save()` 时 invalidate cache
- **验收**: `_gtk_update` 中不再直接调用 `ConfigManager.load()`，改为读缓存

#### R2: Toggle 防抖 (#2)
- **现状**: CheckMenuItem 快速连续点击触发多次 `_save_config_changes(apply_now=True)` → 多次 `backend.restart()`
- **修复**: 加 `_toggle_lock: threading.Lock`，toggle 方法获取锁后检查 config 是否已经处于目标状态
- **验收**: 快速连续点击 CheckMenuItem 不会导致多次 backend.restart()

### P1 — 功能正确性 & UX

#### R3: toggle_voice_wake 补通知 (#1)
- **现状**: `toggle_voice_wake()` 没有通知反馈，其他三个 toggle 都有
- **修复**: 补齐 try/except notify 块，与其他 toggle 保持一致
- **验收**: 切换语音唤醒后收到桌面通知

#### R4: 语音唤醒页瘦身 + 移除死代码 UX (#5)
- **现状**: 语音唤醒 Tab ~40 字段；semantic gate 系列 6 字段在 bd 审查中标记"应移除"
- **修复**:
  - 模型路径（Encoder/Decoder/Joiner/Tokens）移入"高级调优"section
  - semantic gate 字段（wake_use_semantic_gate + 5 子字段）从设置面板**隐藏**，但保留在 save payload 中向后兼容
  - owner 验证字段集中到同一个 section
- **验收**: 唤醒页 visible 字段 ≤ 20；semantic gate 字段不出现在 UI 中

#### R5: 保存反馈明确化 (#7)
- **现状**: 保存后只显示通用消息"已保存并重启后端"，不告诉用户哪些 key 变了
- **修复**: 在状态栏中列出 changed_keys 的中文标签映射
- **验收**: 保存后状态栏显示类似"已保存并重启后端（变更: 文本精炼, 触发热键）"

### P2 — 结构合理性

#### R6: 启动/停止按钮感知后端状态 (#3)
- **现状**: 两个按钮始终可点击
- **修复**: `_update_tray_menu` 中根据 `self.state.backend_running` disabled 空闲/不相关按钮
- **验收**: 后端运行时"启动"按钮 disabled；后端停止时"停止"按钮 disabled

#### R7: 远程粘贴独立 Tab (#6)
- **现状**: 远程粘贴（10+ 字段）和上屏/运行混在"高级"Tab
- **修复**: 远程粘贴独立为第 7 个 Tab "远程粘贴"
- **验收**: 远程粘贴有自己的 Tab 页，不再与上屏/运行混在一起

#### R8: 音效路径加文件选择器 (#9)
- **现状**: sound_on_path / sound_off_path 是普通文本框
- **修复**: 加 Gtk.FileChooserButton 并排
- **验收**: 点击按钮弹出文件选择对话框

#### R9: preset 切换效果一致化 (#10)
- **现状**: tray 快速切换 preset 走 NEXT_SESSION，设置面板保存走 combined_effect（可能重启）
- **修复**: tray preset 切换后也触发 `_save_config_changes(apply_now=True)` 保持一致
- **验收**: 两种入口切换 preset 后行为一致

### P3 — 锦上添花

#### R10: 状态栏信息增强 (#4)
- **现状**: 只显示时间
- **修复**: 有 last_run.text 时显示 "3s ago: 你好世界..."
- **验收**: 状态栏在有识别结果时显示摘要

#### R11: 参数解析提取 (#8)
- **现状**: `open_settings()` L947-1110 有 163 行重复的 try/except validation
- **修复**: 提取为 `_validate_settings_dict(current: dict) -> dict` 函数
- **验收**: `open_settings()` 减少 ~120 行，提取函数有测试覆盖

#### R12: 快速模式概念显化 (#11)
- **现状**: 关闭"启用文本精炼"= 快速模式，但菜单没有体现
- **修复**: CheckMenuItem label 改为 "文本精炼 / 快速模式（关闭时）"，hint 说明
- **验收**: 菜单项标签更清晰

## 约束清单

- 仅修改 `src/recordian/tray_gui.py` 和 `tests/test_tray_gui.py`
- 不修改 backend 核心逻辑（hotkey_dictate.py 等）
- 保持四门全绿（pytest / ruff / mypy / compileall）
- tray_gui.py 当前 3635 行，本次不拆分文件（拆分计划已记录在 bd issue Recordian-8ff）
- 所有 UI 文本为中文
- 不硬编码 API key、路径等

## 术语表

| 术语 | 含义 |
|------|------|
| Toggle | 菜单中的 CheckMenuItem 开关项 |
| preset | 文本精炼的预设配置（.md 文件） |
| semantic gate | 已废弃的语义门控功能（CPU 超标） |
| owner verify | 声纹验证（仅主人声音可触发唤醒） |
| backend | hotkey_dictate 后端进程（由 BackendManager 管理） |
