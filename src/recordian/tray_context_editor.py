from __future__ import annotations

import logging
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any

from recordian.config import ConfigManager
from recordian.setting_effects import effect_status_message
from recordian.tray_utils import export_auto_lexicon_db, import_auto_lexicon_db, save_config_changes

if TYPE_CHECKING:
    from recordian.tray_app import TrayApp

logger = logging.getLogger(__name__)

DEFAULT_AUTO_LEXICON_DB_PATH = "~/.config/recordian/auto_lexicon.db"


def open_context_editor(app: TrayApp) -> None:
    """打开常用词编辑器"""
    if not (hasattr(app, "_glib") and hasattr(app, "_gtk")):
        app.events.put({"event": "log", "message": "GTK 未初始化，无法打开常用词编辑器"})
        return

    Gtk = app._gtk

    def _on_gtk_thread():
        # 检查是否已有窗口打开
        if hasattr(app, "_gtk_context_window") and app._gtk_context_window is not None:
            try:
                app._gtk_context_window.present()
                return False
            except Exception:
                app._gtk_context_window = None

        # 加载当前配置
        try:
            current = ConfigManager.load(app.config_path)
        except Exception as e:
            app.events.put({"event": "log", "message": f"加载配置失败: {e}"})
            return False

        current_context = current.get("asr_context", "")

        # 创建窗口
        win = Gtk.Window(title="常用词管理")
        win.set_default_size(600, 400)
        win.set_position(Gtk.WindowPosition.CENTER)
        win.set_keep_above(True)
        app._gtk_context_window = win

        # 主容器
        root_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        root_box.set_border_width(12)
        win.add(root_box)

        # 标题
        title_label = Gtk.Label()
        title_label.set_xalign(0.0)
        title_label.set_markup("<b>常用词管理</b>")
        root_box.pack_start(title_label, False, False, 0)

        # 说明
        hint_label = Gtk.Label(label="添加常用词可以提高语音识别的准确率。多个词用逗号分隔。")
        hint_label.set_xalign(0.0)
        hint_label.set_opacity(0.75)
        hint_label.set_line_wrap(True)
        root_box.pack_start(hint_label, False, False, 0)

        # 示例
        example_label = Gtk.Label(label="示例: Recordian, Claude, Python, 张三, 李四, 机器学习")
        example_label.set_xalign(0.0)
        example_label.set_opacity(0.6)
        example_label.set_line_wrap(True)
        root_box.pack_start(example_label, False, False, 0)

        # 文本编辑区域
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_shadow_type(Gtk.ShadowType.IN)

        text_view = Gtk.TextView()
        text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        text_view.set_border_width(8)
        text_buffer = text_view.get_buffer()
        text_buffer.set_text(current_context)

        scroll.add(text_view)
        root_box.pack_start(scroll, True, True, 0)

        # 自动词库数据库导入/导出
        auto_db_raw = str(current.get("auto_lexicon_db", DEFAULT_AUTO_LEXICON_DB_PATH)).strip()
        auto_db_path = Path(auto_db_raw or DEFAULT_AUTO_LEXICON_DB_PATH).expanduser()

        db_frame = Gtk.Frame(label="自动词库数据库")
        db_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        db_box.set_border_width(8)
        db_frame.add(db_box)
        root_box.pack_start(db_frame, False, False, 0)

        db_path_label = Gtk.Label(label=f"当前数据库: {auto_db_path}")
        db_path_label.set_xalign(0.0)
        db_path_label.set_line_wrap(True)
        db_path_label.set_opacity(0.8)
        db_box.pack_start(db_path_label, False, False, 0)

        db_hint_label = Gtk.Label(label="导出可备份常用词数据库；导入后建议重启后端以立即刷新内存缓存。")
        db_hint_label.set_xalign(0.0)
        db_hint_label.set_line_wrap(True)
        db_hint_label.set_opacity(0.7)
        db_box.pack_start(db_hint_label, False, False, 0)

        db_btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        db_box.pack_start(db_btn_row, False, False, 0)

        export_btn = Gtk.Button(label="导出数据库…")
        import_btn = Gtk.Button(label="导入数据库…")
        db_btn_row.pack_start(export_btn, False, False, 0)
        db_btn_row.pack_start(import_btn, False, False, 0)

        # 状态标签
        status_label = Gtk.Label()
        status_label.set_xalign(0.0)
        status_label.set_opacity(0.75)
        root_box.pack_start(status_label, False, False, 0)

        def _choose_export_path() -> Path | None:
            dialog = Gtk.FileChooserDialog(
                title="导出常用词数据库",
                parent=win,
                action=Gtk.FileChooserAction.SAVE,
            )
            dialog.add_buttons(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                "导出",
                Gtk.ResponseType.OK,
            )
            dialog.set_do_overwrite_confirmation(True)
            default_name = auto_db_path.name if auto_db_path.name else "auto_lexicon.db"
            dialog.set_current_name(default_name)
            dialog.set_current_folder(str(auto_db_path.parent))
            response = dialog.run()
            selected = Path(dialog.get_filename()).expanduser() if response == Gtk.ResponseType.OK else None
            dialog.destroy()
            return selected

        def _choose_import_path() -> Path | None:
            dialog = Gtk.FileChooserDialog(
                title="导入常用词数据库",
                parent=win,
                action=Gtk.FileChooserAction.OPEN,
            )
            dialog.add_buttons(
                Gtk.STOCK_CANCEL,
                Gtk.ResponseType.CANCEL,
                "导入",
                Gtk.ResponseType.OK,
            )
            if auto_db_path.parent.exists():
                dialog.set_current_folder(str(auto_db_path.parent))
            response = dialog.run()
            selected = Path(dialog.get_filename()).expanduser() if response == Gtk.ResponseType.OK else None
            dialog.destroy()
            return selected

        def _set_status_ok(msg: str) -> None:
            status_label.set_markup(f'<span foreground="green">✓ {msg}</span>')

        def _set_status_error(msg: str) -> None:
            status_label.set_markup(f'<span foreground="red">✗ {msg}</span>')

        def _export_db(*_args: object) -> None:
            try:
                target = _choose_export_path()
                if target is None:
                    return
                export_auto_lexicon_db(auto_db_path, target)
                _set_status_ok(f"已导出数据库到: {target}")
                app.events.put({"event": "log", "message": f"常用词数据库已导出: {target}"})
            except Exception as e:
                _set_status_error(f"导出失败: {e}")

        def _import_db(*_args: object) -> None:
            try:
                source = _choose_import_path()
                if source is None:
                    return
                if source == auto_db_path:
                    _set_status_error("导入源与当前数据库路径相同")
                    return
                import_auto_lexicon_db(source, auto_db_path)
                _set_status_ok("已导入数据库，建议重启后端以立即刷新")
                app.events.put({"event": "log", "message": f"常用词数据库已导入: {source} -> {auto_db_path}"})
            except Exception as e:
                _set_status_error(f"导入失败: {e}")

        export_btn.connect("clicked", _export_db)
        import_btn.connect("clicked", _import_db)

        # 按钮区域
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        button_box.set_halign(Gtk.Align.END)
        root_box.pack_start(button_box, False, False, 0)

        # 取消按钮
        cancel_btn = Gtk.Button(label="取消")
        cancel_btn.connect("activate", lambda _: win.destroy())
        cancel_btn.connect("clicked", lambda _: win.destroy())
        button_box.pack_start(cancel_btn, False, False, 0)

        # 保存按钮
        save_btn = Gtk.Button(label="保存")
        save_btn.get_style_context().add_class("suggested-action")

        def _save_context(*_args):
            try:
                # 获取文本
                start_iter = text_buffer.get_start_iter()
                end_iter = text_buffer.get_end_iter()
                context_text = text_buffer.get_text(start_iter, end_iter, False).strip()

                effect, restarted, _ = save_config_changes(
                    app.config_path,
                    {"asr_context": context_text},
                    apply_now=True,
                    restart_callback=lambda: app.root.after(0, app.backend.restart),
                )

                status_label.set_markup(
                    f'<span foreground="green">✓ {effect_status_message(effect, restarted=restarted)}</span>'
                )
                app.events.put({"event": "log", "message": f"常用词已更新: {context_text[:50]}..."})

                # 1秒后关闭窗口
                def _close_window():
                    try:
                        win.destroy()
                    except Exception:
                        pass
                    return False

                app._glib.timeout_add(1000, _close_window)

            except Exception as e:
                status_label.set_markup(f'<span foreground="red">✗ 保存失败: {e}</span>')

        save_btn.connect("clicked", _save_context)
        button_box.pack_start(save_btn, False, False, 0)

        # 显示窗口
        win.connect("destroy", lambda _: setattr(app, "_gtk_context_window", None))
        win.show_all()
        win.present()
        return False

    app._glib.idle_add(_on_gtk_thread)


__all__ = ["open_context_editor"]
