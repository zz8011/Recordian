from __future__ import annotations

from pathlib import Path
from typing import Any

from recordian.config import ConfigManager
from recordian.preset_manager import PresetManager
from recordian.recommended_profile import DICTATION_BUSY_STATUSES, status_headline
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


def backend_toggle_label(running: bool) -> str:
    return "暂停听写" if running else "开始听写"


def _begin_menu_sync(app: Any) -> None:
    depth = int(getattr(app, "_menu_sync_depth", 0)) + 1
    app._menu_sync_depth = depth
    app._menu_syncing = True


def _end_menu_sync(app: Any) -> None:
    depth = max(0, int(getattr(app, "_menu_sync_depth", 1)) - 1)
    app._menu_sync_depth = depth
    app._menu_syncing = depth > 0


def _menu_syncing(app: Any) -> bool:
    return bool(getattr(app, "_menu_syncing", False))


def _dictation_busy(app: Any) -> bool:
    state = getattr(app, "state", None)
    return str(getattr(state, "status", "")) in DICTATION_BUSY_STATUSES


def _block_restart_while_busy(app: Any) -> bool:
    """Stop a menu action that would restart the service during live dictation."""
    if not _dictation_busy(app):
        return False
    events = getattr(app, "events", None)
    if events is not None and hasattr(events, "put"):
        try:
            events.put({"event": "log", "message": "请先结束当前听写，再保存设置"})
        except Exception:
            pass
    return True


def _set_check_active(app: Any, item: Any, active: bool) -> None:
    if item is None:
        return
    _begin_menu_sync(app)
    try:
        if bool(item.get_active()) != bool(active):
            item.set_active(bool(active))
    finally:
        _end_menu_sync(app)


def _queue_check_restore(app: Any, item: Any, active: bool) -> None:
    """Restore a check item once. Direct when GLib is absent; one-shot idle otherwise."""

    def _restore() -> bool:
        _set_check_active(app, item, active)
        return False

    glib = getattr(app, "_glib", None)
    if glib is None:
        _set_check_active(app, item, active)
        return
    glib.idle_add(_restore)


def build_appindicator_menu(
    app: Any,
    AppIndicator3: Any,
    Gtk: Any,
    GLib: Any,
) -> Any:
    """Build the AppIndicator3 menu with daily actions and an advanced submenu."""
    del AppIndicator3, GLib  # Kept so existing callers pass the GTK modules.

    menu = Gtk.Menu()
    config = app._get_cached_config()

    status_item = Gtk.MenuItem(label=status_summary_label(app.state, config))
    status_item.set_sensitive(False)
    menu.append(status_item)
    app._appindicator_status_item = status_item

    menu.append(Gtk.SeparatorMenuItem())

    backend_toggle_item = Gtk.MenuItem(label=backend_toggle_label(bool(app.state.backend_running)))

    def _on_backend_toggle(_item: Any) -> None:
        if _menu_syncing(app):
            return
        if app.state.backend_running:
            app.root.after(0, app.backend.stop)
        else:
            app.root.after(0, app.backend.start)

    backend_toggle_item.connect("activate", _on_backend_toggle)
    menu.append(backend_toggle_item)
    app._appindicator_backend_toggle_item = backend_toggle_item

    streaming_item = Gtk.CheckMenuItem(label="边说边出字")
    streaming_enabled = bool(config.get("enable_streaming_commit", False))
    _begin_menu_sync(app)
    try:
        streaming_item.set_active(streaming_enabled)
    finally:
        _end_menu_sync(app)

    def _on_streaming_toggled(item: Any) -> None:
        if _menu_syncing(app):
            return
        if _block_restart_while_busy(app):
            wanted = bool(app._get_cached_config().get("enable_streaming_commit", False))
            _queue_check_restore(app, item, wanted)
            return
        app.root.after(0, lambda: app.toggle_streaming_commit(bool(item.get_active())))

    streaming_item.connect("toggled", _on_streaming_toggled)
    menu.append(streaming_item)
    app._appindicator_streaming_item = streaming_item

    menu.append(Gtk.SeparatorMenuItem())

    context_item = Gtk.MenuItem(label="常用词...")
    context_item.connect("activate", lambda _: app.root.after(0, app.open_context_editor))
    menu.append(context_item)

    settings_item = Gtk.MenuItem(label="设置...")
    settings_item.connect("activate", lambda _: app.root.after(0, app.open_settings))
    menu.append(settings_item)

    copy_text_item = Gtk.MenuItem(label="复制上次文字")
    copy_text_item.connect("activate", lambda _: app.root.after(0, app.copy_last_text))
    copy_text_item.set_sensitive(bool(app.state.last_run.text))
    menu.append(copy_text_item)
    app._appindicator_copy_text_item = copy_text_item

    menu.append(Gtk.SeparatorMenuItem())

    more_item = Gtk.MenuItem(label="更多")
    more_submenu = Gtk.Menu()

    text_refine_enabled = bool(config.get("enable_text_refine", False))
    text_refine_item = Gtk.CheckMenuItem(label="文字润色")
    _begin_menu_sync(app)
    try:
        text_refine_item.set_active(text_refine_enabled)
    finally:
        _end_menu_sync(app)

    def _on_refine_toggled(item: Any) -> None:
        if _menu_syncing(app):
            return
        if _block_restart_while_busy(app):
            wanted = bool(app._get_cached_config().get("enable_text_refine", False))
            _queue_check_restore(app, item, wanted)
            return
        app.root.after(0, lambda: app.toggle_text_refine(bool(item.get_active())))

    text_refine_item.connect("toggled", _on_refine_toggled)
    more_submenu.append(text_refine_item)
    app._appindicator_text_refine_item = text_refine_item

    voice_wake_item = Gtk.CheckMenuItem(label="语音唤醒")
    _begin_menu_sync(app)
    try:
        voice_wake_item.set_active(bool(config.get("enable_voice_wake", False)))
    finally:
        _end_menu_sync(app)

    def _on_wake_toggled(item: Any) -> None:
        if _menu_syncing(app):
            return
        if _block_restart_while_busy(app):
            wanted = bool(app._get_cached_config().get("enable_voice_wake", False))
            _queue_check_restore(app, item, wanted)
            return
        app.root.after(0, lambda: app.toggle_voice_wake(bool(item.get_active())))

    voice_wake_item.connect("toggled", _on_wake_toggled)
    more_submenu.append(voice_wake_item)
    app._appindicator_voice_wake_item = voice_wake_item

    preset_menu_item = Gtk.MenuItem(label="润色风格")
    preset_submenu = Gtk.Menu()
    app._appindicator_preset_submenu = preset_submenu
    app._appindicator_preset_menu_item = preset_menu_item
    preset_menu_item.set_submenu(preset_submenu)
    preset_menu_item.set_sensitive(text_refine_enabled)
    more_submenu.append(preset_menu_item)
    refresh_appindicator_preset_submenu(app, Gtk)

    more_submenu.append(Gtk.SeparatorMenuItem())

    speaker_enroll_item = Gtk.MenuItem(label="声纹注册...")
    speaker_enroll_item.connect("activate", lambda _: app.root.after(0, app.open_speaker_enrollment_wizard))
    more_submenu.append(speaker_enroll_item)

    diagnostics_item = Gtk.MenuItem(label="诊断...")
    diagnostics_item.connect("activate", lambda _: app.root.after(0, app.open_diagnostics))
    more_submenu.append(diagnostics_item)

    more_item.set_submenu(more_submenu)
    menu.append(more_item)

    menu.append(Gtk.SeparatorMenuItem())

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
    refine_on = bool(config.get("enable_text_refine", False))
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
    _begin_menu_sync(app)
    try:
        for preset in presets:
            preset_item = Gtk.RadioMenuItem(group=radio_group, label=preset_labels.get(preset, preset))
            if radio_group is None:
                radio_group = preset_item
            if preset == current_preset:
                preset_item.set_active(True)

            def _on_preset_toggled(item: Any, chosen: str = preset) -> None:
                if _menu_syncing(app) or not bool(item.get_active()):
                    return
                cached = app._get_cached_config()
                if not bool(cached.get("enable_text_refine", False)):
                    return
                if _block_restart_while_busy(app):
                    _set_check_active(app, item, False)
                    current_name = str(cached.get("refine_preset", "default")).strip() or "default"
                    current_item = getattr(app, "_appindicator_preset_items", {}).get(current_name)
                    _set_check_active(app, current_item, True)
                    return
                app.root.after(0, lambda name=chosen: app.switch_preset(name))

            preset_item.connect("toggled", _on_preset_toggled)
            preset_submenu.append(preset_item)
            item_map[preset] = preset_item
    finally:
        _end_menu_sync(app)

    preset_menu_item = getattr(app, "_appindicator_preset_menu_item", None)
    if preset_menu_item is not None:
        preset_menu_item.set_sensitive(refine_on)

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
    refine_on = bool(config.get("enable_text_refine", False))
    preset_menu_item = getattr(app, "_appindicator_preset_menu_item", None)
    if preset_menu_item is not None:
        preset_menu_item.set_sensitive(refine_on)
    if not refine_on:
        return
    item = app._appindicator_preset_items.get(current_preset)
    if item is not None and not bool(item.get_active()):
        _set_check_active(app, item, True)


def update_tray_menu(app: Any) -> None:
    """Update AppIndicator status and menu items without writing config."""
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
            cache[status] = cache.get("idle", icon_path)
    icon_path = cache[status]

    glib = getattr(app, "_glib", None)
    if glib is None:
        return

    status_item = getattr(app, "_appindicator_status_item", None)

    def _gtk_update():
        try:
            cfg = app._get_cached_config()
        except Exception:
            cfg = {}
        label = status_summary_label(app.state, cfg)
        if status_item is not None:
            status_item.set_label(label)
        copy_text_item = getattr(app, "_appindicator_copy_text_item", None)
        if copy_text_item is not None:
            copy_text_item.set_sensitive(bool(app.state.last_run.text))
        _set_check_active(
            app,
            getattr(app, "_appindicator_streaming_item", None),
            bool(cfg.get("enable_streaming_commit", False)),
        )
        refine_on = bool(cfg.get("enable_text_refine", False))
        _set_check_active(app, getattr(app, "_appindicator_text_refine_item", None), refine_on)
        _set_check_active(
            app,
            getattr(app, "_appindicator_voice_wake_item", None),
            bool(cfg.get("enable_voice_wake", False)),
        )
        preset_menu_item = getattr(app, "_appindicator_preset_menu_item", None)
        if preset_menu_item is not None:
            preset_menu_item.set_sensitive(refine_on)
        backend_toggle_item = getattr(app, "_appindicator_backend_toggle_item", None)
        if backend_toggle_item is not None:
            backend_toggle_item.set_label(backend_toggle_label(bool(app.state.backend_running)))
        sync_appindicator_preset_submenu(app)
        try:
            indicator.set_icon(icon_path)
        except Exception:
            pass

    glib.idle_add(_gtk_update)


def status_summary_label(state: Any, config: Any = None) -> str:
    """Return the tray headline for the current app state."""
    return status_headline(state, config)


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
    "backend_toggle_label",
    "build_appindicator_menu",
    "refresh_appindicator_preset_submenu",
    "sync_appindicator_preset_submenu",
    "update_tray_menu",
    "status_summary_label",
    "collect_recent_runtime_rows",
]
