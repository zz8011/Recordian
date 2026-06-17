from __future__ import annotations

from pathlib import Path
from typing import Any

from recordian.config import ConfigManager
from recordian.preset_manager import PresetManager
from recordian.tray_utils import truncate


def get_logo_path(status: str) -> Path:
    """Get logo path based on current status."""
    # Get project root (assuming tray_gui.py is in src/recordian/)
    project_root = Path(__file__).parent.parent.parent
    assets_dir = project_root / "assets"

    logo_map = {
        "idle": "logo.png",
        "recording": "logo-recording.png",
        "processing": "logo-recording.png",
        "error": "logo-error.png",
        "stopped": "logo.png",
        "starting": "logo-warming.png",
        "warming": "logo-warming.png",
        "busy": "logo-warming.png",
    }

    logo_file = logo_map.get(status, "logo.png")
    logo_path = assets_dir / logo_file

    if not logo_path.exists():
        # Fallback to default logo
        logo_path = assets_dir / "logo.png"

    return logo_path


def list_tray_refine_presets() -> list[str]:
    """列出托盘菜单可用的文本精炼预设（过滤 asr-* 等非精炼预设）。"""
    preset_manager = PresetManager()
    names = [
        name for name in preset_manager.list_presets()
        if name.lower() != "readme" and not name.lower().startswith("asr-")
    ]
    builtin_order = ["default", "intent", "formal", "meeting", "summary", "technical"]
    builtin = [name for name in builtin_order if name in names]
    custom = sorted(name for name in names if name not in builtin)
    ordered = builtin + custom
    return ordered if ordered else ["default"]


def build_appindicator_menu(
    app: Any,
    AppIndicator3: Any,
    Gtk: Any,
    GLib: Any,
) -> Any:
    """Build the AppIndicator3 menu with all items and callbacks.

    Parameters
    ----------
    app : TrayApp
        The TrayApp instance to bind callbacks to.
    AppIndicator3 : module
        The gi.repository.AppIndicator3 module.
    Gtk : module
        The gi.repository.Gtk module.
    GLib : module
        The gi.repository.GLib module.

    Returns
    -------
    Gtk.Menu
        The constructed menu.
    """
    menu = Gtk.Menu()

    # 状态栏：仅显示时间
    time_label = status_summary_label(app.state)
    status_item = Gtk.MenuItem(label=time_label)
    status_item.set_sensitive(False)
    menu.append(status_item)
    app._appindicator_status_item = status_item

    menu.append(Gtk.SeparatorMenuItem())

    # 启动后端
    start_item = Gtk.MenuItem(label="启动后端")
    start_item.connect("activate", lambda _: app.root.after(0, app.backend.start))
    menu.append(start_item)
    app._appindicator_start_item = start_item

    # 停止后端
    stop_item = Gtk.MenuItem(label="停止后端")
    stop_item.connect("activate", lambda _: app.root.after(0, app.backend.stop))
    menu.append(stop_item)
    app._appindicator_stop_item = stop_item

    menu.append(Gtk.SeparatorMenuItem())

    # Text refine toggle
    text_refine_item = Gtk.CheckMenuItem(label="文本精炼（关闭 = 快速模式）")
    config = app._get_cached_config()
    text_refine_enabled = bool(config.get("enable_text_refine", True))
    text_refine_item.set_active(text_refine_enabled)
    text_refine_item.connect("toggled", lambda item: app.root.after(0, lambda: app.toggle_text_refine(item.get_active())))
    menu.append(text_refine_item)
    app._appindicator_text_refine_item = text_refine_item

    voice_wake_item = Gtk.CheckMenuItem(label="语音唤醒模式")
    voice_wake_enabled = bool(config.get("enable_voice_wake", False))
    voice_wake_item.set_active(voice_wake_enabled)
    voice_wake_item.connect("toggled", lambda item: app.root.after(0, lambda: app.toggle_voice_wake(item.get_active())))
    menu.append(voice_wake_item)
    app._appindicator_voice_wake_item = voice_wake_item

    auto_hard_enter_item = Gtk.CheckMenuItem(label="自动硬回车")
    auto_hard_enter_enabled = bool(config.get("auto_hard_enter", False))
    auto_hard_enter_item.set_active(auto_hard_enter_enabled)
    auto_hard_enter_item.connect("toggled", lambda item: app.root.after(0, lambda: app.toggle_auto_hard_enter(item.get_active())))
    menu.append(auto_hard_enter_item)
    app._appindicator_auto_hard_enter_item = auto_hard_enter_item

    streaming_commit_item = Gtk.CheckMenuItem(label="流式上屏")
    streaming_commit_enabled = bool(config.get("enable_streaming_commit", False))
    streaming_commit_item.set_active(streaming_commit_enabled)
    streaming_commit_item.connect("toggled", lambda item: app.root.after(0, lambda: app.toggle_streaming_commit(item.get_active())))
    menu.append(streaming_commit_item)
    app._appindicator_streaming_commit_item = streaming_commit_item

    # Copy last text
    copy_text_item = Gtk.MenuItem(label="复制最后识别的文本")
    copy_text_item.connect("activate", lambda _: app.root.after(0, app.copy_last_text))
    copy_text_item.set_sensitive(bool(app.state.last_run.text))
    menu.append(copy_text_item)
    app._appindicator_copy_text_item = copy_text_item

    # 预设子菜单
    preset_menu_item = Gtk.MenuItem(label="切换预设")
    preset_submenu = Gtk.Menu()
    app._appindicator_preset_submenu = preset_submenu
    preset_menu_item.set_submenu(preset_submenu)
    menu.append(preset_menu_item)
    refresh_appindicator_preset_submenu(app, Gtk)

    # 常用词管理
    context_item = Gtk.MenuItem(label="常用词管理...")
    context_item.connect("activate", lambda _: app.root.after(0, app.open_context_editor))
    menu.append(context_item)

    # 声纹注册向导
    speaker_enroll_item = Gtk.MenuItem(label="声纹注册向导...")
    speaker_enroll_item.connect("activate", lambda _: app.root.after(0, app.open_speaker_enrollment_wizard))
    menu.append(speaker_enroll_item)

    # 设置
    settings_item = Gtk.MenuItem(label="设置...")
    settings_item.connect("activate", lambda _: app.root.after(0, app.open_settings))
    menu.append(settings_item)

    diagnostics_item = Gtk.MenuItem(label="诊断状态...")
    diagnostics_item.connect("activate", lambda _: app.root.after(0, app.open_diagnostics))
    menu.append(diagnostics_item)

    menu.append(Gtk.SeparatorMenuItem())

    # 退出
    quit_item = Gtk.MenuItem(label="退出")
    quit_item.connect("activate", lambda _: app.root.after(0, app.quit))
    menu.append(quit_item)

    menu.show_all()
    return menu


def refresh_appindicator_preset_submenu(app: Any, Gtk: Any) -> None:
    """重建托盘预设二级菜单，确保与 presets 目录实时联动。"""
    preset_submenu = getattr(app, "_appindicator_preset_submenu", None)
    if Gtk is None or preset_submenu is None:
        return

    for child in list(preset_submenu.get_children()):
        preset_submenu.remove(child)

    presets = list_tray_refine_presets()
    config = ConfigManager.load(app.config_path)
    current_preset = str(config.get("refine_preset", "default")).strip() or "default"
    preset_labels = {
        "default": "默认",
        "intent": "意图整理",
        "formal": "正式",
        "meeting": "会议",
        "summary": "总结",
        "technical": "技术",
    }

    radio_group = None
    item_map: dict[str, Any] = {}
    for preset in presets:
        preset_item = Gtk.RadioMenuItem(group=radio_group, label=preset_labels.get(preset, preset))
        if radio_group is None:
            radio_group = preset_item
        if preset == current_preset:
            preset_item.set_active(True)
        preset_item.connect(
            "activate",
            lambda item, p=preset: app.root.after(0, lambda: app.switch_preset(p)) if item.get_active() else None,
        )
        preset_submenu.append(preset_item)
        item_map[preset] = preset_item

    app._appindicator_preset_items = item_map
    app._appindicator_preset_names = presets
    preset_submenu.show_all()


def sync_appindicator_preset_submenu(app: Any) -> None:
    """同步托盘预设菜单：列表变化时重建，列表不变时仅更新选中项。"""
    import time

    now = time.monotonic()
    if now - getattr(app, "_preset_menu_last_sync_ts", 0.0) < 1.0:
        return
    app._preset_menu_last_sync_ts = now

    presets_now = list_tray_refine_presets()
    if presets_now != getattr(app, "_appindicator_preset_names", []):
        refresh_appindicator_preset_submenu(app, getattr(app, "_gtk", None))
        return

    if not getattr(app, "_appindicator_preset_items", None):
        return

    config = ConfigManager.load(app.config_path)
    current_preset = str(config.get("refine_preset", "default")).strip() or "default"
    item = app._appindicator_preset_items.get(current_preset)
    if item is not None and not bool(item.get_active()):
        item.set_active(True)


def update_tray_menu(app: Any) -> None:
    """Update AppIndicator status and menu items."""
    indicator = getattr(app, "indicator", None)
    if indicator is None:
        return

    status = app.state.status
    cache = getattr(app, "_appindicator_png_cache", {})
    if status not in cache:
        logo_path = get_logo_path(status)
        icon_path = str(logo_path.absolute())
        if logo_path.exists():
            cache[status] = icon_path
        else:
            # Fallback to idle logo
            cache[status] = cache.get("idle", icon_path)
    icon_path = cache[status]

    # Gtk operations must run on the Gtk thread — use GLib.idle_add
    glib = getattr(app, "_glib", None)
    if glib is None:
        return

    status_item = getattr(app, "_appindicator_status_item", None)
    label = status_summary_label(app.state)

    def _gtk_update():
        if status_item is not None:
            status_item.set_label(label)
        # Update copy text item sensitivity
        copy_text_item = getattr(app, "_appindicator_copy_text_item", None)
        if copy_text_item is not None:
            copy_text_item.set_sensitive(bool(app.state.last_run.text))
        cfg = app._get_cached_config()
        text_refine_item = getattr(app, "_appindicator_text_refine_item", None)
        if text_refine_item is not None:
            text_refine_item.set_active(bool(cfg.get("enable_text_refine", True)))
        voice_wake_item = getattr(app, "_appindicator_voice_wake_item", None)
        if voice_wake_item is not None:
            voice_wake_item.set_active(bool(cfg.get("enable_voice_wake", False)))
        auto_hard_enter_item = getattr(app, "_appindicator_auto_hard_enter_item", None)
        if auto_hard_enter_item is not None:
            auto_hard_enter_item.set_active(bool(cfg.get("auto_hard_enter", False)))
        streaming_commit_item = getattr(app, "_appindicator_streaming_commit_item", None)
        if streaming_commit_item is not None:
            streaming_commit_item.set_active(bool(cfg.get("enable_streaming_commit", False)))
        # R6: start/stop button sensitivity
        start_item = getattr(app, "_appindicator_start_item", None)
        if start_item is not None:
            start_item.set_sensitive(not app.state.backend_running)
        stop_item = getattr(app, "_appindicator_stop_item", None)
        if stop_item is not None:
            stop_item.set_sensitive(app.state.backend_running)
        sync_appindicator_preset_submenu(app)
        try:
            indicator.set_icon(icon_path)
        except Exception:
            pass

    glib.idle_add(_gtk_update)


def status_summary_label(state: Any) -> str:
    """Return a short status label for the tray menu status item."""
    observation = state.last_run
    if observation.text:
        return truncate(observation.text, 32)
    if observation.total_ms > 0:
        label = f"时间: {observation.total_ms:.0f} ms"
    else:
        label = "时间: --"
    if observation.detected_language:
        label += f" | 语言: {observation.detected_language}"
    if observation.asr_path:
        label += f" | 路径: {observation.asr_path}"
    return label


def collect_recent_runtime_rows(state: Any) -> list[dict[str, str]]:
    """Collect recent runtime rows from UI state for diagnostics."""
    observation = state.last_run
    rows: list[dict[str, str]] = []
    if observation.asr_path:
        rows.append({"label": "最近 ASR 路径", "status": "info", "detail": observation.asr_path})
    if observation.asr_provider:
        rows.append({"label": "最近 ASR 提供方", "status": "info", "detail": observation.asr_provider})
    if observation.detected_language:
        rows.append({"label": "最近识别语言", "status": "info", "detail": observation.detected_language})
    if observation.asr_capabilities:
        rows.append({"label": "最近 ASR 能力", "status": "info", "detail": observation.asr_capabilities})
    if observation.text:
        rows.append({"label": "最近识别文本", "status": "info", "detail": truncate(observation.text, 80)})
    if observation.total_ms > 0:
        rows.append({"label": "最近耗时", "status": "info", "detail": f"{observation.total_ms:.0f} ms"})
    return rows


__all__ = [
    "get_logo_path",
    "list_tray_refine_presets",
    "build_appindicator_menu",
    "refresh_appindicator_preset_submenu",
    "sync_appindicator_preset_submenu",
    "update_tray_menu",
    "status_summary_label",
    "collect_recent_runtime_rows",
]
