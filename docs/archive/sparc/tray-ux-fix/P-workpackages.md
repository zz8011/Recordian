# P: 工作包分组 & 伪代码

> 基于 S 阶段 12 个需求，按依赖关系分为 3 个并行窗口。

## 工作包分组

### Window 1 — 3 个并行 WP（无相互依赖）

**WP-A: P0 性能修复 (R1 + R2 + R6)**
- R1: ConfigManager 缓存
- R2: Toggle 防抖
- R6: 启动/停止状态感知
- 这三个都涉及 `_update_tray_menu` / `_handle_event` 路径，一起改避免冲突

```
class TrayApp:
    + _config_cache: dict[str, Any] | None = None
    + _config_cache_mtime: float = 0.0
    + _toggle_lock: threading.Lock

    def _get_cached_config() -> dict:
        # 先检查 mtime，没变就返回缓存
        # ConfigManager.save() 后 invalidate

    def _update_tray_menu():
        # 用 _get_cached_config() 替代 ConfigManager.load()
        # 根据 backend_running disabled 启动/停止按钮

    def toggle_text_refine():
        with self._toggle_lock:
            current = self._get_cached_config()
            if current.get("enable_text_refine") == enabled:
                return  # no-op
            # ... save + restart

    # 同理 toggle_voice_wake, toggle_auto_hard_enter, toggle_streaming_commit
```

**TDD 锚点:**
- [TEST] _get_cached_config 首次调用读磁盘
- [TEST] _get_cached_config 重复调用走缓存（mock Path.stat_mtime）
- [TEST] toggle_text_refine 连续调用只触发一次 save
- [TEST] toggle_text_refine 值相同时不触发 save
- [TEST] _update_tray_menu 后端运行时 start disabled

---

**WP-B: P1 UX 修复 (R3 + R5 + R9)**
- R3: voice wake 补通知
- R5: 保存反馈明确化
- R9: preset 切换一致化
- 都涉及 toggle/switch 路径和通知逻辑

```
def toggle_voice_wake():
    # 补 notify 块（与 toggle_text_refine 一致）

def _on_gtk_thread() -> _save():
    # 保存后：
    changed_keys_label = ", ".join(_key_label_map.get(k, k) for k in changed_keys)
    status_label.set_text(f"({changed_keys_label})")

_KEY_LABEL_MAP = {
    "enable_text_refine": "文本精炼",
    "hotkey": "触发热键",
    "trigger_mode": "触发模式",
    "enable_voice_wake": "语音唤醒",
    # ...
}

def switch_preset():
    # 改为 _save_config_changes(apply_now=True) 与设置面板一致
```

**TDD 锚点:**
- [TEST] toggle_voice_wake 调用 notify
- [TEST] 保存后状态栏包含 key 中文标签
- [TEST] switch_preset 触发 _save_config_changes(apply_now=True)

---

**WP-C: P2 结构调整 (R7 + R4)**
- R7: 远程粘贴独立 Tab
- R4: 唤醒页瘦身 + 隐藏 semantic gate
- 都涉及设置面板的 Tab/Section 重组

```
# R7: 远程粘贴独立 Tab
tab_remote = _create_tab("远程粘贴")
sec_remote = _create_section(tab_remote, "远程粘贴")
# 把原来 tab_advanced 里的 sec_remote 内容移过来

# R4: 唤醒页瘦身
# 1. 把模型路径字段从 sec_wake_model 移到 sec_wake_advanced
# 2. 隐藏 semantic gate 相关 6 字段（不创建 UI，但保留在 save payload）
# 3. owner 验证字段集中到 sec_wake_main

# 隐藏字段列表（不在 _add_field 中创建，但在 _save 时保留原值）:
HIDDEN_SETTINGS = {
    "wake_use_semantic_gate",
    "wake_semantic_probe_interval_s",
    "wake_semantic_window_s",
    "wake_semantic_end_silence_s",
    "wake_semantic_min_chars",
    "wake_semantic_timeout_ms",
}
```

**TDD 锚点:**
- [TEST] 远程粘贴字段在独立 Tab 中
- [TEST] semantic gate 字段不出现在 entries dict（不在 UI 中创建）
- [TEST] save payload 仍包含 hidden settings 的原值

---

### Window 2 — 3 个并行 WP（依赖 Window 1 不冲突）

**WP-D: P2 音效文件选择器 (R8)**
```
# sound_on_path / sound_off_path 旁加 FileChooserButton
row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
entry = Gtk.Entry()
chooser = Gtk.FileChooserButton(title="选择音效文件")
chooser.set_filename(str(Path(value).expanduser()))
chooser.connect("file-set", lambda w: entry.set_text(w.get_filename()))
row_box.pack_start(entry, True, True, 0)
row_box.pack_start(chooser, False, False, 0)
```

**TDD 锚点:**
- [TEST] sound_on_path 和 sound_off_path 字段旁边有 file chooser 按钮

---

**WP-E: P3 状态栏增强 + 快速模式标签 (R10 + R12)**
```
def _status_summary_label(state: UiState):
    if state.last_run.text:
        ago = time.time() - state.last_run.timestamp
        return f"{int(ago)}s ago: {_truncate(state.last_run.text, 24)}"
    return _format_time(state.last_updated)

# CheckMenuItem label
text_refine_item = Gtk.CheckMenuItem(label="文本精炼（关闭 = 快速模式）")
```

**TDD 锚点:**
- [TEST] 有 last_run.text 时状态栏显示摘要
- [TEST] 无 last_run.text 时显示时间
- [TEST] CheckMenuItem label 包含"快速模式"

---

**WP-F: P3 参数解析提取 (R11)**
```
def _validate_settings_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Extract and validate all ~100 settings from raw config dict."""
    validated = {}
    # 把 open_settings() L947-1110 的 validation 逻辑搬到这里
    # 返回 validated dict
    return validated

def open_settings():
    current = _load_hotkey_default_config(include_sound_defaults=True)
    current.update(ConfigManager.load(self.config_path))
    current = _validate_settings_dict(current)
    # ... 后续不变
```

**TDD 锚点:**
- [TEST] _validate_settings_dict 正确 clamp 所有数值范围
- [TEST] _validate_settings_dict 处理缺失 key 用默认值
- [TEST] _validate_settings_dict 处理类型错误

---

## 任务依赖图

```
WP-A (R1+R2+R6)  ─┐
WP-B (R3+R5+R9)  ─┼─→ Window 2: WP-D, WP-E, WP-F (并行)
WP-C (R7+R4)     ─┘
                        │
                        └─→ Window 3: C 集成验证
```

Window 1 三个 WP 互相独立可完全并行。Window 2 三个 WP 只需等 Window 1 的文件写入完成，互相间也独立。
