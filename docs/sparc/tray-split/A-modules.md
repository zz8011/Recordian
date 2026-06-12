# A-modules.md — Recordian tray_gui 拆分架构

## 目标
将 `src/recordian/tray_gui.py`（3563 行）拆分为 7 个模块，保持现有行为不变，测试 566 passed 全绿。

## 模块划分

### 1. tray_utils.py — 纯工具函数
- **职责**：无 GTK 依赖、无业务状态、无副作用的纯工具函数
- **包含**：
  - `_overlay_hide_delay_seconds()` — 波形覆盖层延迟计算
  - `_next_event_poll_delay_ms()` — 托盘轮询退避
  - `_sqlite_backup()` — SQLite 在线备份
  - `_export_auto_lexicon_db()` / `_import_auto_lexicon_db()` — 词库导入导出
  - `_truncate()` — 文本截断
  - `_normalize_hotkey_token()` / `_format_hotkey_spec()` / `_build_gtk_hotkey_spec()` — 热键格式化
  - `_parse_bool()` — 布尔值解析
  - `_hex_with_alpha()` / `_blend_hex()` — 颜色工具
- **导出**：全部公开（无 `_` 前缀的版本），`tray_gui.py` 通过 `from .tray_utils import ...` 引用
- **依赖**：无内部模块依赖，仅标准库 + `WaveformRenderer`

### 2. tray_menu.py — 菜单构建与 AppIndicator
- **职责**：GTK 菜单构建、AppIndicator 创建、菜单项回调绑定
- **包含**：
  - `get_logo_path()` — 根据状态返回图标路径
  - `_build_menu()` — 从 TrayApp 提取的菜单构建逻辑
  - `_on_menu_show()` — 菜单显示前刷新状态
- **导出**：`get_logo_path`, `build_menu`, `on_menu_show`
- **依赖**：`tray_utils`（`get_logo_path` 用 `_hex_with_alpha`）
- **注意**：菜单回调（如 `on_settings`, `on_context_editor`）通过闭包或 dependency injection 传入，不直接 import TrayApp

### 3. tray_settings.py — 设置面板（~500 行，可接受）
- **职责**：GTK 设置窗口的完整实现
- **包含**：
  - `_open_settings_gtk()` — 主设置窗口（1500+ 行，保留在此）
  - `_save_config_changes()` — 配置保存
  - `_derive_openai_models_endpoint()` — 端点推导
  - `_fetch_json_url()` — HTTP 获取
  - `_describe_provider_capabilities()` — 提供商能力描述
  - `_create_asr_provider_for_diagnostics()` — 诊断用 ASR 创建
  - `_load_hotkey_default_config()` — 热键默认配置加载
- **导出**：`open_settings_gtk`, `save_config_changes`
- **依赖**：`tray_utils`（`_sqlite_backup`, `_fetch_json_url` 等）
- **边界**：这是最大模块，但 `_open_settings_gtk` 是单一 GTK 窗口的完整实现，按 Tab 再拆会增加模块间耦合，当前保留

### 4. tray_context_editor.py — 上下文编辑器
- **职责**：上下文编辑 GTK 窗口
- **包含**：`_open_context_editor()` 从 TrayApp 提取
- **导出**：`open_context_editor`
- **依赖**：`tray_utils`

### 5. tray_diagnostics.py — 诊断功能
- **职责**：运行时诊断收集与报告格式化
- **包含**：
  - `collect_runtime_diagnostics()` — 收集诊断信息
  - `_format_diagnostic_report()` — 格式化报告
- **导出**：`collect_runtime_diagnostics`, `format_diagnostic_report`
- **依赖**：`tray_utils`

### 6. tray_speaker_wizard.py — 说话人注册向导
- **职责**：说话人注册 GTK 向导
- **包含**：`_open_speaker_wizard()` 从 TrayApp 提取
- **导出**：`open_speaker_wizard`
- **依赖**：`tray_utils`

### 7. tray_app.py — TrayApp 主类 + main()
- **职责**：保留 TrayApp 类的事件循环、状态管理、回调注册，以及 `main()` 入口
- **包含**：
  - `TrayApp` 类（精简版，方法委托给各模块）
  - `RecentRunObservation` 数据类
  - `UiState` 数据类
  - `_extract_recent_run_observation()` — 运行时结果提取
  - `_format_recent_run_log_suffix()` — 日志后缀格式化
  - `_status_summary_label()` — 状态摘要标签
  - `_collect_recent_runtime_rows()` — 运行时行收集
  - `main()` — 入口函数
- **导出**：`TrayApp`, `main`
- **依赖**：全部 6 个新模块 + 原 `tray_settings_utils`, `audio_feedback`, `backend_manager` 等

## 数据流

```
main() → TrayApp.__init__() → 加载配置 → 启动后端 → 创建 AppIndicator
         ↓
    用户点击菜单 → tray_menu.build_menu() 回调 → TrayApp 方法
         ↓
    设置 → tray_settings.open_settings_gtk() → 修改配置 → _save_config_changes()
         ↓
    上下文 → tray_context_editor.open_context_editor()
         ↓
    诊断 → tray_diagnostics.collect_runtime_diagnostics()
         ↓
    向导 → tray_speaker_wizard.open_speaker_wizard()
         ↓
    事件循环 → TrayApp._poll_tray_events() → 更新 UI 状态
```

## 接口契约

### tray_utils → 无依赖
```python
def overlay_hide_delay_seconds(overlay: WaveformRenderer, state: str, detail: str) -> float: ...
def next_event_poll_delay_ms(*, handled_events: int) -> int: ...
def sqlite_backup(src_path: Path, dst_path: Path) -> None: ...
def export_auto_lexicon_db(db_path: Path, export_path: Path) -> None: ...
def import_auto_lexicon_db(import_path: Path, db_path: Path) -> None: ...
def truncate(text: str, max_len: int) -> str: ...
def normalize_hotkey_token(raw: str) -> str: ...
def format_hotkey_spec(*, modifiers: set[str], key: str) -> str: ...
def build_gtk_hotkey_spec(event: object, gdk: Any) -> str: ...
def parse_bool(value: str, *, default: bool) -> bool: ...
def hex_with_alpha(color: str, alpha: float) -> str: ...
def blend_hex(a: str, b: str, ratio: float) -> str: ...
```

### tray_menu → 依赖 tray_utils
```python
def get_logo_path(status: str) -> Path: ...
def build_menu(
    app: TrayApp,  # 或回调字典，避免循环依赖
    on_settings: Callable,
    on_context_editor: Callable,
    on_diagnostics: Callable,
    on_speaker_wizard: Callable,
    on_quit: Callable,
) -> Any: ...  # 返回 GTK 菜单对象
def on_menu_show(app: TrayApp) -> None: ...
```

### tray_settings → 依赖 tray_utils
```python
def open_settings_gtk(app: TrayApp) -> None: ...
def save_config_changes(config: ConfigManager, changes: dict[str, Any]) -> None: ...
```

### tray_context_editor → 依赖 tray_utils
```python
def open_context_editor(app: TrayApp) -> None: ...
```

### tray_diagnostics → 依赖 tray_utils
```python
def collect_runtime_diagnostics(config: Mapping[str, Any]) -> list[dict[str, str]]: ...
def format_diagnostic_report(rows: list[dict[str, str]]) -> str: ...
```

### tray_speaker_wizard → 依赖 tray_utils
```python
def open_speaker_wizard(app: TrayApp) -> None: ...
```

### tray_app → 依赖全部
```python
class TrayApp:
    def __init__(self, args: argparse.Namespace) -> None: ...
    def _poll_tray_events(self) -> None: ...
    # ... 其他保留方法

def main() -> None: ...
```

## 技术选型
- **模块间通信**：函数参数传递，避免全局状态
- **TrayApp 引用**：各模块函数接收 `app: TrayApp` 参数，但 `tray_app.py` 最后 import 各模块（避免循环依赖）
- **测试策略**：保持现有测试不变，新增模块的测试在后续迭代补充；当前目标：566 测试全绿

## 验收标准
- [ ] `tray_gui.py` 被删除，7 个新模块文件存在
- [ ] `from recordian.tray_gui import main` 仍然工作（通过 `tray_app.py` 的 re-export）
- [ ] 566 测试 passed，0 failed
- [ ] 运行时行为不变（启动、设置、录音、退出）
