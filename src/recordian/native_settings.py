#!/usr/bin/env python3
"""On-demand GTK settings with an editable snapshot and atomic persistence."""

from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import gi

from recordian.desktop_preferences import DEFAULTS as DESKTOP_DEFAULTS
from recordian.desktop_preferences import DesktopPreferencesStore
from recordian.recommended_profile import DICTATION_BUSY_STATUSES
from recordian.refine_model_discovery import fetch_model_list
from recordian.setting_effects import SettingEffect, combined_setting_effect, effect_status_message
from recordian.settings_catalog import discover_agents, language_choices, microphone_choices
from recordian.settings_draft import SettingsDraft
from recordian.tray_settings import build_gtk_hotkey_spec, load_hotkey_default_config

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

ROOT = Path(__file__).resolve().parent
_CSS_PROVIDER = None
PAGES = [
    ("preferences", "⌂", "偏好设置", "让 Recordian 适合你的日常使用。"),
    ("daily", "◎", "日常听写", "从开始说话，到文字落在光标处。"),
    ("input", "⌁", "文字与输入", "决定文字如何进入应用，也照顾你的常用词。"),
    ("asr", "◈", "识别服务", "选择识别来源，再配置对应的连接。"),
    ("refine", "≋", "文字润色", "本机或远端，统一通过 API 连接。"),
    ("hotkeys", "⌘", "快捷键", "为听写入口配置按键。"),
    ("wake", "◌", "语音唤醒与声纹", "唤醒方式与声纹验证放在一起。"),
    ("agent", "◇", "Agent", "发现本机程序，再选择任务入口。"),
    ("recording", "≡", "录音参数", "低频采集参数，按服务要求配置。"),
    ("diagnostics", "⊙", "运行与诊断", "运行选项和配置检查集中在这里。"),
]


def apply_settings_style(window):
    """One scoped provider shared by settings and its auxiliary GTK panels."""
    global _CSS_PROVIDER
    styled(window, "recordian-settings")
    if _CSS_PROVIDER is None:
        _CSS_PROVIDER = Gtk.CssProvider()
        _CSS_PROVIDER.load_from_path(str(ROOT / "native_settings.css"))
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(), _CSS_PROVIDER, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )
    window.set_icon_from_file(str(ROOT / "ui_assets/recordian-app-64.png"))



def styled(widget, *classes):
    for cls in classes:
        widget.get_style_context().add_class(cls)
    return widget


def label(text, cls=None):
    w = Gtk.Label(label=text)
    w.set_xalign(0)
    if cls:
        styled(w, cls)
    return w


def box(vertical=True, spacing=0):
    return Gtk.Box(orientation=Gtk.Orientation.VERTICAL if vertical else Gtk.Orientation.HORIZONTAL, spacing=spacing)


class NativeSettingsWindow:
    def __init__(self, app, current, page="preferences"):
        self.app = app
        defaults = load_hotkey_default_config(include_sound_defaults=True)
        # These preferences belong to AgentHub rather than the ASR parser.
        defaults.setdefault("enable_agent", True)
        defaults.setdefault("wake_to_agent", True)
        fields = (
            "trigger_mode input_device qwen_language enable_streaming_commit auto_hard_enter "
            "enable_agent wake_to_agent commit_backend asr_context asr_provider asr_realtime_endpoint "
            "asr_endpoint qwen_model device asr_timeout_s enable_text_refine refine_provider "
            "refine_api_base refine_api_model refine_model wake_owner_verify "
            "hotkey toggle_hotkey "
            "stop_hotkey cooldown_ms enable_voice_wake wake_prefix wake_name wake_auto_stop_silence_s "
            "record_backend sample_rate channels duration warmup debug_diagnostics capture_refine_samples"
        ).split()

        def form(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, (list, tuple)):
                return ", ".join(str(item) for item in value)
            return "" if value is None else str(value)

        # Hidden legacy model paths are preserved even by form defaults.
        defaults["refine_model"] = current.get("refine_model", defaults.get("refine_model"))
        self.draft = SettingsDraft(
            {key: form(current.get(key, defaults.get(key))) for key in fields},
            {key: form(defaults.get(key)) for key in fields},
        )
        self.extension_store = getattr(app, "settings_extension_store", None)
        self.extension_error = ""
        try:
            if self.extension_store is None and not getattr(app, "settings_preview", False):
                self.extension_store = DesktopPreferencesStore(Path(app.config_path).with_name("agents.json"))
            extra = self.extension_store.load() if self.extension_store else {}
        except (ValueError, OSError):
            self.extension_store = None
            self.extension_error = "桌面偏好未加载：Agent 配置无效或无法读取，原文件保留。"
            extra = {}
        extension_defaults = dict(DESKTOP_DEFAULTS)
        # Restore the form without resetting hidden profile identity or paths.
        extension_defaults.update({k: v for k, v in extra.items() if k.startswith("agent_")})
        self.extension = SettingsDraft(extra, extension_defaults)
        self.controls = {}
        self.extension_controls = {}
        self.agent_candidates = discover_agents()
        self.inventory_generation = 0
        self.saved_refine_key = str(current.get("refine_api_key") or "").strip()
        self.discovery_generation = 0
        self.discovery_running = False
        self.closed = False
        self.syncing = False
        self.groups = {}
        self.nav = {}
        self.page_id = page
        self.window = Gtk.Window(title="Recordian · 设置")
        apply_settings_style(self.window)
        self.window.set_default_size(1100, 940)
        self.window.set_size_request(960, 760)
        root = styled(box(), "root")
        self.window.add(root)
        top = styled(box(False, 12), "topbar")
        root.pack_start(top, False, False, 0)
        mark = Gtk.Image.new_from_pixbuf(
            GdkPixbuf.Pixbuf.new_from_file_at_scale(str(ROOT / "ui_assets/recordian-mark-golden.svg"), -1, 32, True)
        )
        self.window.set_icon_from_file(str(ROOT / "ui_assets/recordian-app-64.png"))
        top.pack_start(mark, False, False, 0)
        brand = box(spacing=3)
        brand.pack_start(label("Recordian", "brand"), False, False, 0)
        brand.pack_start(label("SETTINGS  /  设置", "eyebrow"), False, False, 0)
        top.pack_start(brand, True, True, 0)
        self.close_button = styled(Gtk.Button(label="×"), "window-close")
        self.close_button.set_tooltip_text("关闭设置")
        self.close_button.get_accessible().set_name("关闭设置")
        self.close_button.connect("clicked", lambda _: self.dismiss())
        top.pack_end(self.close_button, False, False, 0)
        if getattr(app, "settings_preview", False):
            top.pack_end(label("隔离预览 · 合成配置", "badge"), False, False, 0)
        body = box(False)
        root.pack_start(body, True, True, 0)
        sidebar = styled(box(spacing=4), "sidebar")
        sidebar.set_size_request(210, -1)
        body.pack_start(sidebar, False, False, 0)
        heading = label("偏好设置", "section-title")
        heading.set_margin_start(16)
        heading.set_margin_bottom(8)
        sidebar.pack_start(heading, False, False, 0)
        for ident, icon, name, _ in PAGES:
            if ident == "recording":
                sep = Gtk.Separator()
                sep.set_margin_top(14)
                sep.set_margin_bottom(12)
                sidebar.pack_start(sep, False, False, 0)
            btn = styled(Gtk.Button(), "nav")
            line = box(False, 10)
            line.pack_start(label(icon, "nav-icon"), False, False, 0)
            line.pack_start(label(name), False, False, 0)
            btn.add(line)
            btn.connect("clicked", lambda _, i=ident: self.navigate(i))
            sidebar.pack_start(btn, False, False, 0)
            self.nav[ident] = btn
        foot = box(spacing=5)
        foot.set_margin_start(16)
        foot.set_valign(Gtk.Align.END)
        foot.set_vexpand(True)
        foot.pack_start(label("关闭后继续听写", "sidebar-note"), False, False, 0)
        sidebar.pack_end(foot, False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        body.pack_start(self.stack, True, True, 0)
        page_order = sorted(PAGES, key=lambda item: item[0] != page)
        for ident, _, name, sub in page_order:
            scroll = Gtk.ScrolledWindow()
            scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            pagebox = styled(box(), "page")
            scroll.add(pagebox)
            pagebox.pack_start(label(name, "page-title"), False, False, 0)
            subtitle = label(sub, "page-subtitle")
            subtitle.set_margin_top(9)
            pagebox.pack_start(subtitle, False, False, 0)
            self.groups[ident] = pagebox
            self.stack.add_named(scroll, ident)
        self.build_pages()
        footer = styled(box(False, 10), "footer")
        root.pack_end(footer, False, False, 0)
        self.status = label("当前设置 · 修改后点击保存", "status")
        self.status.set_line_wrap(True)
        footer.pack_start(self.status, True, True, 0)
        restore = Gtk.Button(label="恢复默认…")
        restore.connect("clicked", self.restore_confirm)
        footer.pack_start(restore, False, False, 0)
        self.cancel = Gtk.Button(label="取消修改")
        self.cancel.connect("clicked", lambda _: self.cancel_changes())
        footer.pack_start(self.cancel, False, False, 0)
        self.save = styled(Gtk.Button(label="保存草稿" if getattr(app, "settings_preview", False) else "保存设置"), "primary")
        self.save.connect("clicked", lambda _: self.save_changes())
        footer.pack_start(self.save, False, False, 0)
        self.window.connect("delete-event", self.close_request)
        self.window.connect("destroy", self.destroyed)
        self.window.show_all()
        self.navigate(page)
        self.update()
        if not getattr(app, "settings_preview", False):
            self.refresh_microphones()
            self.refresh_autostart()

    def section(self, page, title):
        parent = self.groups[page]
        parent.pack_start(label(title, "section-title"), False, False, 0)
        group = styled(box(), "group")
        parent.pack_start(group, False, False, 0)
        return group

    def row(self, group, key, title, hint, kind="entry", choices=()):
        if group.get_children():
            group.pack_start(Gtk.Separator(), False, False, 0)
        row = styled(box(False, 16), "row")
        left = box(spacing=5)
        left.set_hexpand(True)
        left.pack_start(label(title, "row-title"), False, False, 0)
        note = label(hint, "row-hint")
        note.set_line_wrap(True)
        note.set_max_width_chars(45)
        note.set_width_chars(35)
        left.pack_start(note, False, False, 0)
        row.pack_start(left, True, True, 0)
        draft = self.extension if key in self.extension.values else self.draft
        value = self.draft.refine_key if kind == "secret" else draft.values[key]
        if kind == "switch":
            w = Gtk.Switch()
            w.set_active(value)
            w.set_valign(Gtk.Align.FILL if kind == "text" else Gtk.Align.CENTER)
            w.connect("notify::active", lambda widget, _: self.changed(key, widget.get_active()))
        elif kind == "segments":
            w = styled(box(False), "segments")
            buttons = []
            for val, text in choices:
                btn = styled(Gtk.ToggleButton(label=text), "segment")
                btn.set_active(value == val)
                btn.connect("clicked", lambda _, v=val: self.changed(key, v))
                w.pack_start(btn, False, False, 0)
                buttons.append((val, btn))
            w._segments = buttons
        elif kind == "agentcombo":
            model = Gtk.ListStore(str, str, bool)
            for candidate in self.agent_candidates:
                model.append([candidate.name + (" · 已发现" if candidate.executable else " · 手动配置") +
                              ("" if candidate.supported else " · 暂未支持"), candidate.kind, candidate.supported])
            w = Gtk.ComboBox.new_with_model(model)
            w.set_id_column(1)
            cell = Gtk.CellRendererText()
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            cell.set_property("max-width-chars", 28)
            w.pack_start(cell, True)
            w.add_attribute(cell, "text", 0)
            w.add_attribute(cell, "sensitive", 2)
            w.set_active_id(value)
            w.connect("changed", lambda widget: self.changed(key, widget.get_active_id()))
        elif kind == "combo":
            w = Gtk.ComboBoxText()
            for val, text in choices:
                w.append(val, text)
            w.set_active_id(value)
            for cell in w.get_cells():
                cell.set_property("ellipsize", Pango.EllipsizeMode.END)
                cell.set_property("max-width-chars", 28)
            w.connect("changed", lambda widget: self.changed(key, widget.get_active_id()))
        elif kind == "text":
            w = Gtk.TextView()
            w.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
            w.set_left_margin(14)
            w.set_right_margin(14)
            w.set_top_margin(12)
            w.set_bottom_margin(12)
            w.get_buffer().set_text(value)
            w.get_buffer().connect("changed", lambda buffer: self.changed(
                key, buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), True)))
        elif kind == "model":
            w = Gtk.ComboBoxText.new_with_entry()
            entry = w.get_child()
            entry.set_width_chars(23)
            entry.set_text(value)
            entry.set_placeholder_text("选择或手动输入模型")
            entry.connect("changed", lambda widget: self.changed(key, widget.get_text()))
        else:
            w = Gtk.Entry()
            w.set_text(value)
            w.set_width_chars(23 if kind not in ("number", "hotkey") else 10)
            if key == "refine_api_base":
                w.set_width_chars(31)
            if not value:
                w.set_placeholder_text("未设置")
            if kind == "secret":
                w.set_visibility(False)
                w.set_input_purpose(Gtk.InputPurpose.PASSWORD)
                w.set_placeholder_text("留空保留已保存 Key" if self.saved_refine_key else "可选 · 输入 API Key")
            if kind == "hotkey":
                w.connect("key-press-event", lambda widget, event: self.capture_key(widget, event))
            w.connect("changed", lambda widget: self.changed(key, widget.get_text()))
        w.set_valign(Gtk.Align.FILL if kind == "text" else Gtk.Align.CENTER)
        if kind == "text":
            row.remove(left)
            row.set_orientation(Gtk.Orientation.VERTICAL)
            row.set_spacing(12)
            row.pack_start(left, False, False, 0)
            scroller = styled(Gtk.ScrolledWindow(), "text-editor")
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(240)
            scroller.set_max_content_height(390)
            scroller.set_propagate_natural_height(True)
            scroller.add(w)
            row.pack_start(scroller, True, True, 0)
        else:
            row.pack_end(w, False, False, 0)
        group.pack_start(row, False, False, 0)
        (self.extension_controls if draft is self.extension else self.controls)[key] = (kind, w)
        return row

    def notice(self, page, text):
        note = label(text, "notice")
        note.set_line_wrap(True)
        note.set_max_width_chars(70)
        note.set_margin_top(20)
        self.groups[page].pack_start(note, False, False, 0)

    def action_row(self, group, title, hint, action, callback):
        if group.get_children():
            group.pack_start(Gtk.Separator(), False, False, 0)
        row = styled(box(False, 16), "row")
        left = box(spacing=6)
        left.pack_start(label(title, "row-title"), False, False, 0)
        note = label(hint, "row-hint")
        note.set_line_wrap(True)
        note.set_max_width_chars(48)
        left.pack_start(note, False, False, 0)
        row.pack_start(left, True, True, 0)
        button = Gtk.Button(label=action)
        button.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", callback)
        row.pack_end(button, False, False, 0)
        group.pack_start(row, False, False, 0)
        return button

    def build_pages(self):
        s = self.section("preferences", "桌面与启动")
        self.row(s, "start_on_login", "开机自启", "登录桌面时启动 Recordian。", "switch")
        self.startup_note = label(
            "原型中的开机自启只保存草稿，不修改桌面服务。" if getattr(self.app, "settings_preview", False)
            else self.extension_error or "正在读取当前桌面启动状态…", "notice")
        self.startup_note.set_line_wrap(True)
        self.startup_note.set_margin_top(20)
        self.groups["preferences"].pack_start(self.startup_note, False, False, 0)
        if not getattr(self.app, "settings_preview", False):
            self.extension_controls["start_on_login"][1].set_sensitive(False)
        s = self.section("preferences", "常用设置")
        self.action_row(s, "开始与结束", "麦克风、语言和文字上屏。", "日常听写 →", lambda _: self.navigate("daily"))
        self.action_row(s, "文字优化", "连接 API、识别模型、选择润色方式。", "文字润色 →", lambda _: self.navigate("refine"))
        s = self.section("daily", "开始听写")
        self.row(
            s,
            "trigger_mode",
            "说话方式",
            "按住键开始，松开结束；也可用开关模式。",
            "segments",
            [("ptt", "按住说话"), ("toggle", "点按开关"), ("oneshot", "录一段")],
        )
        self.row(s, "input_device", "麦克风", "选择当前录音后端的输入；默认项跟随系统。", "combo",
                 [("default", "跟随系统默认麦克风"), *([(self.draft.values["input_device"], "已配置 · 本次未检测到")]
                   if self.draft.values["input_device"] != "default" else [])])
        self.action_row(s, "设备列表", "只枚举设备，不打开麦克风。", "刷新设备", lambda _: self.refresh_microphones())
        self.row(s, "qwen_language", "识别语言", "随识别服务调整；网络服务的旧语言值保留。", "combo",
                 language_choices(self.draft.values["asr_provider"], self.draft.values["qwen_language"]))
        s = self.section("daily", "文字上屏")
        self.row(s, "enable_streaming_commit", "边说边出字", "需要流式服务与兼容的输入后端。", "switch")
        self.row(s, "auto_hard_enter", "说完自动回车", "文字上屏后再按回车，可能直接发送。", "switch")
        self.notice("daily", "按键在「快捷键」中编辑，识别来源在「识别服务」中选择。")
        s = self.section("input", "输入路径")
        self.row(
            s,
            "commit_backend",
            "输入后端",
            "Fcitx 支持预编辑；其他方式依现有输入实现。",
            "combo",
            [
                ("fcitx", "Fcitx 输入法"),
                ("auto", "自动"),
                ("wtype", "wtype"),
                ("xdotool", "xdotool"),
                ("xdotool-clipboard", "xdotool · 剪贴板"),
                ("stdout", "标准输出"),
                ("none", "不输出"),
            ],
        )
        s = self.section("input", "常用词与显式替换")
        self.row(s, "asr_context", "常用词提示", "用逗号或换行分隔；显式替换写成「错词 → 正词」。", "text")
        self.action_row(s, "自动词库", "导入与导出已有词库数据库。", "管理词库…", lambda _: self.open_word_tools())
        self.notice("input", "提示词参与识别偏置和上屏前纠正。修改后统一保存；自动学习词库由独立工具管理。")
        s = self.section("asr", "识别来源")
        self.row(
            s,
            "asr_provider",
            "识别服务",
            "本机流式、本机模型或网络服务。",
            "combo",
            [("confucius-asr", "流式 Confucius"), ("qwen-asr", "本机 Qwen"), ("http-cloud", "网络识别服务")],
        )
        s = self.section("asr", "连接与模型")
        self.asr_rows = {}
        self.asr_rows["ws"] = self.row(
            s, "asr_realtime_endpoint", "实时服务地址", "Confucius 使用 ws/wss；网络识别使用 http/https 或留空。"
        )
        self.asr_rows["http"] = self.row(s, "asr_endpoint", "HTTP 服务地址", "网络识别请求使用的端点。")
        self.asr_rows["model"] = self.row(s, "qwen_model", "本机模型", "使用已配置模型；本窗口不执行下载。")
        self.asr_rows["device"] = self.row(
            s,
            "device",
            "计算设备",
            "auto 由现有运行时选择。",
            "combo",
            [("auto", "自动"), ("cpu", "CPU"), ("cuda", "CUDA")],
        )
        self.row(s, "asr_timeout_s", "识别超时 · 秒", "请求超过此时间后结束等待。", "number")
        self.notice("asr", "本窗口保留已保存凭据，不提供密钥编辑或连通性测试。")
        s = self.section("refine", "可选文字处理")
        self.row(s, "enable_text_refine", "启用文字润色", "识别后再处理文字，需要已配置的模型服务。", "switch")
        self.legacy_refine = box(spacing=8)
        styled(self.legacy_refine, "notice")
        self.legacy_refine.pack_start(label("当前沿用旧的进程内模型配置", "row-title"), False, False, 0)
        self.legacy_refine.pack_start(label("原值保留。已有 API 服务后，可明确迁移连接方式。", "row-hint"), False, False, 0)
        migrate = Gtk.Button(label="改为 API 连接")
        migrate.set_halign(Gtk.Align.START)
        migrate.connect("clicked", lambda _: self.changed("refine_provider", "cloud"))
        self.legacy_refine.pack_start(migrate, False, False, 0)
        self.groups["refine"].pack_start(self.legacy_refine, False, False, 16)
        # Keep the legacy provider in the snapshot, but offer one API connection
        # in the UI. Only an explicit migration changes the provider.
        s = self.section("refine", "模型连接")
        self.refine_rows = {}
        self.refine_rows["refine_api_base"] = self.row(
            s, "refine_api_base", "API 地址", "可包含端口，例如 http://主机:端口/v1。"
        )
        self.refine_rows["refine_api_key"] = self.row(
            s, "refine_api_key", "API Key", "隐藏显示；留空保留已保存 Key。", "secret"
        )
        self.refine_rows["refine_api_model"] = self.row(
            s, "refine_api_model", "润色模型", "从识别结果中选择，也可手动输入模型标识。", "model"
        )
        discovery = styled(box(False, 12), "row")
        self.discovery_status = label("点击识别，从此 API 获取模型列表。", "row-hint")
        self.discovery_status.set_line_wrap(True)
        self.discovery_status.set_max_width_chars(48)
        discovery.pack_start(self.discovery_status, True, True, 0)
        self.discover_button = Gtk.Button(label="识别模型")
        self.discover_button.set_valign(Gtk.Align.CENTER)
        self.discover_button.connect("clicked", self.discover_models)
        discovery.pack_end(self.discover_button, False, False, 0)
        s.pack_start(Gtk.Separator(), False, False, 0)
        s.pack_start(discovery, False, False, 0)
        self.refine_rows["discovery"] = discovery
        self.notice("refine", "同一个 API 地址可指向本机或远端。识别按钮只读取模型列表；原型预览不发送网络请求。")
        s = self.section("hotkeys", "听写按键")
        for k, t, h in [
            ("hotkey", "按住说话", "点击后按键即可编辑。"),
            ("toggle_hotkey", "点按开关", "留空沿用现有自动选择行为。"),
            ("stop_hotkey", "结束录音", "允许与按住说话使用同一键。"),
        ]:
            self.row(s, k, t, h, "hotkey")
        s = self.section("hotkeys", "触发保护")
        self.row(s, "cooldown_ms", "触发间隔 · 毫秒", "避免连续触发；不代表识别延迟。", "number")
        self.notice("hotkeys", "输入框聚焦时接收按键；Tab 切换焦点，Delete 清空。\n由现有后端在设置生效时注册快捷键。")
        s = self.section("wake", "唤醒入口")
        self.row(s, "enable_voice_wake", "启用语音唤醒", "需要已配置的本地唤醒模型。", "switch")
        self.row(s, "wake_prefix", "唤醒前缀", "多个前缀用逗号分隔。")
        self.row(s, "wake_name", "唤醒称呼", "多个称呼用逗号分隔。")
        self.row(s, "wake_auto_stop_silence_s", "静音结束 · 秒", "说话后静音多久结束录音。", "number")
        s = self.section("wake", "声纹验证")
        self.row(s, "wake_owner_verify", "启用声纹验证", "需要先完成声纹注册；沿用现有验证实现。", "switch")
        self.action_row(s, "声纹档案", "使用独立向导录制三段样本；完成注册会独立保存档案。", "声纹注册…", lambda _: self.open_enrollment())
        self.notice("wake", "此页不自动录音。未显示的唤醒模型、VAD 和声纹路径保持原值。")
        s = self.section("agent", "语音任务入口")
        self.row(s, "enable_agent", "启用 Agent", "沿用现有 Agent 功能开关，保存后生效。", "switch")
        self.row(s, "wake_to_agent", "唤醒进入 Agent", "需要已有语音唤醒和 Agent 配置。", "switch")
        s = self.section("agent", "本机 Agent")
        profiles = self.extension_store.profiles() if hasattr(self.extension_store, "profiles") else []
        choices = [*profiles, ("new", "新建实例")]
        self.row(s, "agent_instance", "默认任务实例", "选择已有实例，或新建一个配置；不启动任务。", "combo", choices)
        self.row(s, "agent_client", "Agent 程序", "已接通的适配可选择；其他发现的程序置灰。", "agentcombo")
        self.row(s, "agent_id", "实例标识", "小写英文、数字、横线或下划线。")
        self.row(s, "agent_name", "显示名称", "用于任务入口中的实例名称。")
        self.transport_row = self.row(s, "agent_transport", "连接方式", "Hermes 可选 CLI / 本机 Gateway；Claude Code 使用非交互 CLI。", "combo", [("cli", "CLI"), ("gateway", "Hermes Gateway")])
        self.row(s, "agent_executable", "程序路径", "可手动覆盖自动发现的路径。")
        self.row(s, "agent_workspace", "工作目录", "使用存在的项目目录；保存配置不启动任务。")
        self.agent_home_row = self.row(s, "agent_home", "Hermes 配置目录", "CLI 可留空；Gateway 需要已有的 profile 目录。")
        self.gateway_row = self.row(s, "agent_api_url", "Gateway 地址", "现有协议只接受本机 HTTP 地址与端口。")
        self.row(s, "agent_timeout_s", "任务超时 · 秒", "允许 30–7200 秒，不改变现有任务。", "number")
        self.agent_capability = label("", "row-hint")
        self.agent_capability.set_line_wrap(True)
        self.agent_capability.set_margin_start(16)
        self.agent_capability.set_margin_end(16)
        self.agent_capability.set_margin_bottom(16)
        s.pack_start(self.agent_capability, False, False, 0)
        self.action_row(s, "程序发现", "检查 PATH 与常见 bin 目录，不读取 Agent 的个人配置。", "重新搜索", lambda _: self.refresh_agents())
        self.notice("agent", "原型预览只保存临时草稿。正式设置复用 agents.json，运行层在空闲任务边界载入，不中断正在执行的任务。支持 Hermes 和 Claude Code；其余 CLI 的执行适配尚未接入。")
        s = self.section("recording", "录音参数")
        self.row(
            s,
            "record_backend",
            "录音后端",
            "沿用现有音频采集实现。",
            "combo",
            [("auto", "自动选择"), ("ffmpeg-pulse", "PulseAudio · ffmpeg"), ("arecord", "ALSA · arecord")],
        )
        self.row(s, "sample_rate", "采样率 · Hz", "需与识别服务兼容。", "number")
        self.row(s, "channels", "声道数", "按现有服务的输入要求配置。", "number")
        self.row(s, "duration", "单次录音上限 · 秒", "一次性录音模式使用的上限。", "number")
        s = self.section("diagnostics", "运行选项")
        self.row(s, "warmup", "启动时预热", "后端启动时执行已有模型预热逻辑。", "switch")
        self.row(s, "debug_diagnostics", "调试诊断", "开启后可能产生额外诊断输出。", "switch")
        self.row(s, "capture_refine_samples", "采集润色样本", "开启后会保存涉及听写文字的样本。", "switch")
        self.notice("recording", "采集参数需与所选录音后端和识别服务一致；修改后按现有后端策略生效。")
        s = self.section("diagnostics", "配置检查")
        self.action_row(s, "检查当前草稿", "只校验表单，不请求模型、不读取听写记录。", "检查配置", lambda _: self.check_draft())
        self.check_status = label("尚未检查", "row-hint")
        self.check_status.set_margin_start(16)
        self.check_status.set_margin_bottom(16)
        self.check_status.set_line_wrap(True)
        s.pack_start(self.check_status, False, False, 0)
        self.notice("diagnostics", "诊断输出与样本采集默认遵循现有配置；样本可能包含听写文字。")

    def navigate(self, page):
        self.page_id = page
        self.stack.set_visible_child_name(page)
        for ident, w in self.nav.items():
            (w.get_style_context().add_class if ident == page else w.get_style_context().remove_class)("active")

    def dismiss(self):
        if not self.close_request():
            self.window.destroy()

    def check_draft(self):
        errors = self.draft.errors()
        self.check_status.set_text("表单有效 · 未进行设备或服务测试" if not errors else "；".join(errors.values()))

    def open_word_tools(self):
        if self.draft.values["asr_context"] != self.draft.saved["asr_context"]:
            self.status.set_text("请先保存或取消常用词修改，再管理自动词库。")
            return
        callback = getattr(self.app, "open_context_editor", None)
        if callback:
            callback()

    def open_enrollment(self):
        if self.draft.dirty:
            self.status.set_text("请先保存或取消表单修改，再注册声纹。")
            return
        callback = getattr(self.app, "open_speaker_enrollment_wizard", None)
        if callback:
            callback()

    def refresh_autostart(self):
        store = self.extension_store
        if not hasattr(store, "refresh_autostart"):
            self.startup_note.set_text(self.extension_error or "当前桌面启动方式不可配置；原状态保留。")
            return
        def query():
            enabled = store.refresh_autostart()
            def apply():
                if self.closed:
                    return False
                self.syncing = True
                if enabled is not None:
                    self.extension.saved["start_on_login"] = enabled
                    self.extension.values["start_on_login"] = enabled
                    self.extension_controls["start_on_login"][1].set_active(enabled)
                self.extension_controls["start_on_login"][1].set_sensitive(enabled is not None)
                self.syncing = False
                self.startup_note.set_text("保存只改变下次登录时的启动方式，不启动或停止当前进程。" if enabled is not None else "未发现可配置的已安装桌面服务；原状态保留。")
                self.update()
                return False
            GLib.idle_add(apply)
        Thread(target=query, daemon=True, name="recordian-startup-status").start()

    def refresh_agents(self):
        self.agent_candidates = discover_agents()
        widget = self.extension_controls["agent_client"][1]
        model = widget.get_model()
        self.syncing = True
        model.clear()
        for candidate in self.agent_candidates:
            model.append([candidate.name + (" · 已发现" if candidate.executable else " · 手动配置") +
                          ("" if candidate.supported else " · 暂未支持"), candidate.kind, candidate.supported])
        widget.set_active_id(self.extension.values["agent_client"])
        self.syncing = False
        if not self.extension.values["agent_executable"]:
            candidate = next(c for c in self.agent_candidates if c.kind == self.extension.values["agent_client"])
            if candidate.executable:
                self.changed("agent_executable", candidate.executable)
                self.extension_controls["agent_executable"][1].set_text(candidate.executable)
        self.update()
        self.status.set_text("已检查本机 Agent 程序路径 · 未启动任何 CLI")

    def refresh_microphones(self):
        self.inventory_generation += 1
        generation = self.inventory_generation
        backend = self.draft.values["record_backend"]
        current = self.draft.values["input_device"]
        def fetch():
            choices = microphone_choices(backend, current)
            def apply():
                if self.closed or generation != self.inventory_generation:
                    return False
                value = self.draft.values["input_device"]
                if value not in dict(choices):
                    choices.append((value, "已配置 · 本次未检测到"))
                self.set_choices("input_device", choices, value)
                return False
            GLib.idle_add(apply)
        Thread(target=fetch, daemon=True, name="recordian-device-inventory").start()

    def set_choices(self, key, choices, value):
        widget = self.controls[key][1]
        self.syncing = True
        widget.remove_all()
        for ident, name in choices:
            widget.append(ident, name)
        widget.set_active_id(value)
        self.syncing = False

    def changed(self, key, value):
        if self.syncing:
            return
        if key == "refine_api_key":
            self.draft.set_refine_key(value)
        elif key in self.extension.values:
            if key == "agent_client" and not any(c.kind == value and c.supported for c in self.agent_candidates):
                self.syncing = True
                self.extension_controls[key][1].set_active_id(self.extension.values[key])
                self.syncing = False
                self.status.set_text("此 CLI 已发现，但执行适配尚未支持。")
                return
            self.extension.set(key, value)
            if key == "agent_instance" and hasattr(self.extension_store, "select_profile"):
                for field, field_value in self.extension_store.select_profile(value).items():
                    self.extension.set(field, field_value)
                self.sync()
            if key == "agent_client":
                candidate = next(c for c in self.agent_candidates if c.kind == value)
                self.extension.set("agent_executable", candidate.executable)
                self.extension.set("agent_transport", "cli")
                self.extension.set("agent_home", "")
                self.extension.set("agent_api_url", "")
                self.syncing = True
                self.extension_controls["agent_executable"][1].set_text(candidate.executable)
                self.syncing = False
                self.sync()
        else:
            self.draft.set(key, value)
        if key == "asr_provider":
            self.set_choices("qwen_language", language_choices(value, self.draft.values["qwen_language"]), self.draft.values["qwen_language"])
        if key == "record_backend":
            self.refresh_microphones()
        if key in ("refine_api_base", "refine_api_key", "refine_provider"):
            self.invalidate_discovery()
        self.update()

    def invalidate_discovery(self):
        self.discovery_generation += 1
        combo = self.controls["refine_api_model"][1]
        # Choices belong to a connection; preserve the manually edited value.
        value = self.draft.values["refine_api_model"]
        self.syncing = True
        combo.remove_all()
        combo.get_child().set_text(value)
        self.syncing = False
        self.discovery_status.set_text("连接已更新 · 点击识别获取模型列表。")

    def discover_models(self, *_):
        if self.closed or self.discovery_running or self.draft.values["refine_provider"] != "cloud":
            return
        base = self.draft.values["refine_api_base"].strip()
        try:
            parsed = urlsplit(base)
            valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and parsed.port != 0
            valid = valid and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            self.discovery_status.set_text("请填写有效 API 基址，如 http://主机:端口/v1。")
            return
        if getattr(self.app, "settings_preview", False):
            combo = self.controls["refine_api_model"][1]
            value = self.draft.values["refine_api_model"]
            self.syncing = True
            combo.remove_all()
            for model in ["example-model", "example-instruct"]:
                combo.append_text(model)
            combo.get_child().set_text(value)
            self.syncing = False
            self.discovery_status.set_text("预览示例 · 2 个合成模型，未连接任何服务。")
            return
        key = self.draft.refine_key or self.saved_refine_key
        generation = self.discovery_generation
        self.discovery_running = True
        self.discover_button.set_sensitive(False)
        self.discovery_status.set_text("正在识别模型…")

        def fetch():
            try:
                models = fetch_model_list(base, key, timeout_s=8.0)
            except Exception:
                models = []

            def apply():
                self.discovery_running = False
                if self.closed:
                    return False
                self.discover_button.set_sensitive(True)
                if generation != self.discovery_generation:
                    return False
                if models:
                    # Discovery uses OpenAI's /v1 endpoint. Keep the saved
                    # endpoint on that same protocol for the runtime refiner.
                    normalized = base.rstrip("/")
                    if not normalized.endswith("/v1"):
                        normalized += "/v1"
                    self.draft.set("refine_api_base", normalized)
                    combo = self.controls["refine_api_model"][1]
                    value = self.draft.values["refine_api_model"]
                    self.syncing = True
                    self.controls["refine_api_base"][1].set_text(normalized)
                    combo.remove_all()
                    for model in models:
                        combo.append_text(model)
                    combo.get_child().set_text(value)
                    self.syncing = False
                    self.update()
                    self.discovery_status.set_text(f"已识别 {len(models)} 个模型 · 请在下拉框中选择。")
                else:
                    self.discovery_status.set_text("未获取模型 · 检查地址、Key 和服务，或手动输入模型。")
                return False

            GLib.idle_add(apply)

        Thread(target=fetch, daemon=True, name="recordian-model-discovery").start()

    def update(self):
        if not hasattr(self, "save"):
            return
        dirty = self.draft.dirty or self.extension.dirty
        self.save.set_sensitive(dirty)
        self.cancel.set_sensitive(dirty)
        self.status.set_text(
            "有未保存的修改 · 保存后按现有策略生效" if dirty else "当前设置 · 修改后点击保存"
        )
        for _, w in self.controls.values():
            w.get_style_context().remove_class("error")
        provider = self.draft.values["asr_provider"]
        for key, row in self.asr_rows.items():
            row.set_visible(
                key
                in (
                    {"ws"}
                    if provider == "confucius-asr"
                    else {"model", "device"}
                    if provider == "qwen-asr"
                    else {"ws", "http"}
                )
            )
        api = self.draft.values["refine_provider"] == "cloud"
        self.legacy_refine.set_visible(not api)
        for row in self.refine_rows.values():
            row.set_visible(api)
        candidate = next((c for c in self.agent_candidates if c.kind == self.extension.values["agent_client"]), None)
        self.agent_capability.set_text(
            ("已发现程序" if candidate and candidate.executable else "未发现程序，可手动填写路径") + " · " +
            ("支持 Hermes CLI / 本机 Gateway" if candidate and candidate.kind == "hermes"
             else "支持 Claude Code · 非交互 JSON；工具权限沿用 CLI 配置" if candidate and candidate.kind == "claude"
             else "任务执行适配尚未接入")
        )
        hermes = self.extension.values["agent_client"] == "hermes"
        self.transport_row.set_visible(hermes)
        self.agent_home_row.set_visible(hermes)
        self.gateway_row.set_visible(hermes and self.extension.values["agent_transport"] == "gateway")
        self.syncing = True
        for k, (kind, w) in self.controls.items():
            if kind == "segments":
                for val, btn in w._segments:
                    btn.set_active(self.draft.values[k] == val)
        self.syncing = False

    def sync(self):
        self.syncing = True
        for k, (kind, w) in {**self.controls, **self.extension_controls}.items():
            val = self.draft.refine_key if kind == "secret" else (self.extension.values if k in self.extension.values else self.draft.values)[k]
            if kind == "switch":
                w.set_active(val)
            elif kind in ("combo", "agentcombo"):
                w.set_active_id(val)
            elif kind == "text":
                w.get_buffer().set_text(val)
            elif kind == "model":
                w.get_child().set_text(val)
            elif kind == "segments":
                for item, btn in w._segments:
                    btn.set_active(item == val)
            else:
                w.set_text(val)
        self.syncing = False
        self.set_choices("qwen_language", language_choices(self.draft.values["asr_provider"], self.draft.values["qwen_language"]), self.draft.values["qwen_language"])
        self.update()

    def save_changes(self):
        errors = self.draft.errors()
        if errors:
            key = next(iter(errors))
            if key == "refine_model" and key not in self.controls:
                self.navigate("refine")
                self.status.set_text("旧进程内模型配置缺少模型路径，请先迁移到已配置的 API 服务。")
                return False
            w = self.controls[key][1]
            styled(w, "error")
            w.grab_focus()
            for ident, scroll in [(p[0], self.stack.get_child_by_name(p[0])) for p in PAGES]:
                if w.is_ancestor(scroll):
                    self.navigate(ident)
                    break
            self.status.set_text(f"未保存：{errors[key]}（{key}）")
            return False
        effect = combined_setting_effect(list(self.draft.changes()))
        if str(self.app.state.status) in DICTATION_BUSY_STATUSES and effect is SettingEffect.RESTART_REQUIRED:
            self.status.set_text("请先结束当前听写，再保存设置")
            return False
        if self.extension.dirty and self.extension_store is None:
            self.status.set_text(self.extension_error or "桌面偏好未加载，本次尚未保存。")
            return False
        extension_before = None
        extension_written = False
        receipt = None
        try:
            if self.extension_store and hasattr(self.extension_store, "validate"):
                self.extension_store.validate(self.extension.values)
            extension_before = self.extension_store.load() if self.extension_store else None
            if self.extension.dirty:
                receipt = self.extension_store.save(self.extension.values)
                extension_written = True
            replacement_key = self.draft.refine_key
            effect, restarted, _ = self.draft.persist(
                self.app.config_path,
                apply_now=True,
                status=str(self.app.state.status),
                restart_callback=lambda: self.app.root.after(0, self.app.backend.restart),
            )
        except ValueError as exc:
            if extension_written:
                try:
                    self.extension_store.restore(receipt) if receipt is not None else self.extension_store.save(extension_before)
                except Exception:
                    self.status.set_text("保存失败，附加设置未能回退；请核对配置与自启状态。")
                    return False
            self.status.set_text("未保存：" + str(exc))
            return False
        except Exception:
            if extension_written:
                try:
                    self.extension_store.restore(receipt) if receipt is not None else self.extension_store.save(extension_before)
                except Exception:
                    self.status.set_text("保存失败，附加设置未能回退；请核对配置与自启状态。")
                    return False
            self.status.set_text("保存失败，原配置保留。请检查文件写入权限。")
            return False
        if self.extension_store:
            self.extension = SettingsDraft(self.extension.values, self.extension.defaults)
        self.app._invalidate_config_cache()
        self.app._update_tray_menu()
        if replacement_key:
            self.saved_refine_key = replacement_key
            self.controls["refine_api_key"][1].set_placeholder_text("留空保留已保存 Key")
        self.sync()
        self.status.set_text(
            "已保存隔离草稿 · 未改变真实配置或服务" if getattr(self.app, "settings_preview", False)
            else "已保存，正在请求后端重启" if restarted
            else "已保存 Agent 配置 · 空闲任务边界载入" if receipt is not None and receipt.agent_changed
            else effect_status_message(effect, restarted=False)
        )
        return True

    def cancel_changes(self):
        self.draft.cancel()
        self.extension.cancel()
        self.invalidate_discovery()
        self.sync()
        self.status.set_text("已取消修改 · 配置文件未改变")

    def restore_confirm(self, _):
        dlg = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="恢复此表单的默认设置？",
        )
        dlg.format_secondary_text("只更改本表单显示的字段，保存前不会写入配置。凭据和其他字段保持原值。")
        dlg.add_button("取消", Gtk.ResponseType.CANCEL)
        dlg.add_button("恢复默认", Gtk.ResponseType.OK)
        answer = dlg.run()
        dlg.destroy()
        if answer == Gtk.ResponseType.OK:
            self.draft.restore()
            self.extension.restore()
            self.invalidate_discovery()
            self.sync()

    def close_request(self, *_):
        if not (self.draft.dirty or self.extension.dirty):
            return False
        dlg = Gtk.MessageDialog(
            transient_for=self.window,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="设置有未保存的修改",
        )
        dlg.format_secondary_text("关闭后放弃本次修改，已保存的配置不受影响。")
        dlg.add_button("继续编辑", Gtk.ResponseType.CANCEL)
        dlg.add_button("放弃并关闭", Gtk.ResponseType.OK)
        answer = dlg.run()
        dlg.destroy()
        return answer != Gtk.ResponseType.OK

    def destroyed(self, *_):
        self.closed = True
        self.inventory_generation += 1
        self.saved_refine_key = ""
        self.draft.refine_key = ""
        if getattr(self.app, "_gtk_settings_window", None) is self.window:
            self.app._gtk_settings_window = None
            self.app._native_settings = None

    def capture_key(self, w, event):
        name = (Gdk.keyval_name(event.keyval) or "").lower()
        if name in ("tab", "iso_left_tab"):
            return False
        if name in ("delete", "backspace"):
            w.set_text("")
            return True
        spec = build_gtk_hotkey_spec(event, Gdk)
        if spec:
            w.set_text(spec)
        return True


def open_settings_gtk(app, *, current, page="preferences", **_legacy):
    """Schedule creation or raise the existing settings window on the GTK loop."""

    def show():
        existing = getattr(app, "_gtk_settings_window", None)
        if existing is not None:
            app._native_settings.navigate(page)
            existing.present()
            return False
        ui = NativeSettingsWindow(app, current, page=page)
        app._native_settings = ui
        app._gtk_settings_window = ui.window
        ui.window.present()
        return False

    app._glib.idle_add(show)
