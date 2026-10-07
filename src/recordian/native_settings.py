#!/usr/bin/env python3
"""On-demand GTK settings with an editable snapshot and atomic persistence."""

from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit

import gi

from recordian.refine_model_discovery import fetch_model_list
from recordian.setting_effects import effect_status_message
from recordian.settings_draft import SettingsDraft
from recordian.tray_settings import build_gtk_hotkey_spec, load_hotkey_default_config

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk  # noqa: E402

ROOT = Path(__file__).resolve().parent
_CSS_PROVIDER = None
PAGES = [
    ("daily", "◎", "日常听写", "从开始说话，到文字落在光标处。"),
    ("input", "⌁", "文字与输入", "决定识别结果如何进入应用。"),
    ("asr", "◈", "识别服务", "选择识别来源，再配置对应的连接。"),
    ("refine", "≋", "文字润色", "可选的文字处理，需要配置模型服务。"),
    ("remote", "↗", "远程粘贴", "将文字送到另一台电脑，需要接收端。"),
    ("hotkeys", "⌘", "快捷键", "为现有听写入口配置按键。"),
    ("wake", "◌", "语音唤醒", "可选的唤醒入口，需要本地唤醒模型。"),
    ("advanced", "≡", "高级设置", "低频参数集中在这里，日常听写保持简单。"),
]


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
    def __init__(self, app, current, page="daily"):
        self.app = app
        defaults = load_hotkey_default_config(include_sound_defaults=True)
        fields = (
            "trigger_mode input_device qwen_language enable_streaming_commit auto_hard_enter "
            "enable_agent wake_to_agent commit_backend asr_context asr_provider asr_realtime_endpoint "
            "asr_endpoint qwen_model device asr_timeout_s enable_text_refine refine_provider "
            "refine_api_base refine_api_model refine_model enable_remote_paste remote_paste_host remote_paste_port "
            "remote_paste_timeout_s remote_paste_follow_deskflow_active_screen hotkey toggle_hotkey "
            "stop_hotkey cooldown_ms enable_voice_wake wake_prefix wake_name wake_auto_stop_silence_s "
            "record_backend sample_rate channels duration warmup debug_diagnostics capture_refine_samples"
        ).split()

        def form(value):
            if isinstance(value, bool):
                return value
            if isinstance(value, (list, tuple)):
                return ", ".join(str(item) for item in value)
            return "" if value is None else str(value)

        self.draft = SettingsDraft(
            {key: form(current.get(key, defaults.get(key))) for key in fields},
            {key: form(defaults.get(key)) for key in fields},
        )
        self.controls = {}
        self.saved_refine_key = str(current.get("refine_api_key") or "").strip()
        self.discovery_generation = 0
        self.discovery_running = False
        self.closed = False
        self.syncing = False
        self.groups = {}
        self.nav = {}
        self.page_id = page
        self.window = Gtk.Window(title="Recordian · 设置")
        self.window.get_style_context().add_class("recordian-settings")
        self.window.set_default_size(1040, 900)
        self.window.set_size_request(920, 700)
        global _CSS_PROVIDER
        if _CSS_PROVIDER is None:
            _CSS_PROVIDER = Gtk.CssProvider()
            _CSS_PROVIDER.load_from_path(str(ROOT / "native_settings.css"))
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), _CSS_PROVIDER, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
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
        top.pack_end(label("偏好设置", "badge"), False, False, 0)
        body = box(False)
        root.pack_start(body, True, True, 0)
        sidebar = styled(box(spacing=4), "sidebar")
        sidebar.set_size_request(195, -1)
        body.pack_start(sidebar, False, False, 0)
        heading = label("偏好设置", "section-title")
        heading.set_margin_start(16)
        heading.set_margin_bottom(8)
        sidebar.pack_start(heading, False, False, 0)
        for ident, icon, name, _ in PAGES:
            if ident == "advanced":
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
        foot.pack_start(label("GTK 3 · 按需打开", "sidebar-note"), False, False, 0)
        foot.pack_start(label("关闭后继续听写", "sidebar-note"), False, False, 0)
        sidebar.pack_end(foot, False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        body.pack_start(self.stack, True, True, 0)
        for ident, _, name, sub in PAGES:
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
        self.save = styled(Gtk.Button(label="保存并生效"), "primary")
        self.save.connect("clicked", lambda _: self.save_changes())
        footer.pack_start(self.save, False, False, 0)
        self.window.connect("delete-event", self.close_request)
        self.window.connect("destroy", self.destroyed)
        self.window.show_all()
        self.navigate(page)
        self.update()

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
        value = self.draft.refine_key if kind == "secret" else self.draft.values[key]
        if kind == "switch":
            w = Gtk.Switch()
            w.set_active(value)
            w.set_valign(Gtk.Align.CENTER)
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
        elif kind == "combo":
            w = Gtk.ComboBoxText()
            for val, text in choices:
                w.append(val, text)
            w.set_active_id(value)
            w.connect("changed", lambda widget: self.changed(key, widget.get_active_id()))
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
        w.set_valign(Gtk.Align.CENTER)
        w.set_tooltip_text(f"配置字段：{key}")
        row.pack_end(w, False, False, 0)
        group.pack_start(row, False, False, 0)
        self.controls[key] = (kind, w)
        return row

    def notice(self, page, text):
        note = label(text, "notice")
        note.set_line_wrap(True)
        note.set_max_width_chars(70)
        note.set_margin_top(20)
        self.groups[page].pack_start(note, False, False, 0)

    def build_pages(self):
        s = self.section("daily", "开始听写")
        self.row(
            s,
            "trigger_mode",
            "说话方式",
            "按住键开始，松开结束；也可用开关模式。",
            "segments",
            [("ptt", "按住说话"), ("toggle", "点按开关"), ("oneshot", "录一段")],
        )
        self.row(s, "input_device", "麦克风", "default 跟随系统；也可填写现有设备名称。")
        self.row(s, "qwen_language", "识别语言", "语言支持取决于所选服务；可填写服务支持的语言。")
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
        self.row(s, "asr_context", "常用词提示", "填写传给识别服务的提示词。")
        self.notice("input", "词库、显式替换和润色预设保留托盘菜单中的独立编辑入口。")
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
        self.row(
            s,
            "refine_provider",
            "润色服务",
            "现有本机模型、云端 HTTP 和 llama.cpp 接口。",
            "combo",
            [("local", "本机模型"), ("cloud", "云端 HTTP"), ("llamacpp", "本机 llama.cpp")],
        )
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
        self.refine_rows["refine_model"] = self.row(
            s, "refine_model", "本机模型名称或路径", "本机填模型名称；llama.cpp 填 GGUF 路径。"
        )
        self.notice(
            "refine",
            "识别只读取兼容 API 的模型列表，不运行模型。选中后点击「保存并生效」。\n润色预设仍在托盘菜单中管理。",
        )
        s = self.section("remote", "另一台电脑")
        self.row(s, "enable_remote_paste", "启用远程粘贴", "远端需运行与 Recordian 兼容的接收端。", "switch")
        self.row(s, "remote_paste_host", "远程主机", "填写已配置接收端的主机地址。")
        self.row(s, "remote_paste_port", "接收端口", "请与接收端设置一致。", "number")
        self.row(s, "remote_paste_timeout_s", "发送超时 · 秒", "等待远端确认的时间。", "number")
        self.row(
            s,
            "remote_paste_follow_deskflow_active_screen",
            "跟随 Deskflow 活跃屏幕",
            "需要已有 Deskflow 集成与屏幕配置。",
            "switch",
        )
        self.notice("remote", "已保存的配对信息保持原值。此窗口不连接接收端或发送文字。")
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
        s = self.section("wake", "Agent 入口")
        self.row(s, "enable_agent", "启用 Agent", "沿用现有 Agent 功能开关，保存后生效。", "switch")
        self.row(s, "wake_to_agent", "唤醒进入 Agent", "需要已有语音唤醒和 Agent 配置。", "switch")
        self.notice("wake", "未显示的模型路径、声纹和 VAD 参数保持原值。此窗口不监听麦克风。")
        s = self.section("advanced", "录音参数")
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
        s = self.section("advanced", "运行与诊断")
        self.row(s, "warmup", "启动时预热", "后端启动时执行已有模型预热逻辑。", "switch")
        self.row(s, "debug_diagnostics", "调试诊断", "开启后可能产生额外诊断输出。", "switch")
        self.row(s, "capture_refine_samples", "采集润色样本", "开启后会保存涉及听写文字的样本。", "switch")
        self.notice("advanced", "多数参数保存后需要重启听写后端。录音或处理期间禁止需要重启的保存。")

    def navigate(self, page):
        self.page_id = page
        self.stack.set_visible_child_name(page)
        for ident, w in self.nav.items():
            (w.get_style_context().add_class if ident == page else w.get_style_context().remove_class)("active")

    def changed(self, key, value):
        if self.syncing:
            return
        if key == "refine_api_key":
            self.draft.set_refine_key(value)
        else:
            self.draft.set(key, value)
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
                    combo = self.controls["refine_api_model"][1]
                    value = self.draft.values["refine_api_model"]
                    self.syncing = True
                    combo.remove_all()
                    for model in models:
                        combo.append_text(model)
                    combo.get_child().set_text(value)
                    self.syncing = False
                    self.discovery_status.set_text(f"已识别 {len(models)} 个模型 · 请在下拉框中选择。")
                else:
                    self.discovery_status.set_text("未获取模型 · 检查地址、Key 和服务，或手动输入模型。")
                return False

            GLib.idle_add(apply)

        Thread(target=fetch, daemon=True, name="recordian-model-discovery").start()

    def update(self):
        if not hasattr(self, "save"):
            return
        self.save.set_sensitive(self.draft.dirty)
        self.cancel.set_sensitive(self.draft.dirty)
        self.status.set_text(
            "有未保存的修改 · 保存后按现有策略生效" if self.draft.dirty else "当前设置 · 修改后点击保存"
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
        for key, row in self.refine_rows.items():
            row.set_visible((key == "refine_model") != (self.draft.values["refine_provider"] == "cloud"))
        self.syncing = True
        for k, (kind, w) in self.controls.items():
            if kind == "segments":
                for val, btn in w._segments:
                    btn.set_active(self.draft.values[k] == val)
        self.syncing = False

    def sync(self):
        self.syncing = True
        for k, (kind, w) in self.controls.items():
            val = self.draft.refine_key if kind == "secret" else self.draft.values[k]
            if kind == "switch":
                w.set_active(val)
            elif kind == "combo":
                w.set_active_id(val)
            elif kind == "model":
                w.get_child().set_text(val)
            elif kind == "segments":
                for item, btn in w._segments:
                    btn.set_active(item == val)
            else:
                w.set_text(val)
        self.syncing = False
        self.update()

    def save_changes(self):
        errors = self.draft.errors()
        if errors:
            key = next(iter(errors))
            w = self.controls[key][1]
            styled(w, "error")
            w.grab_focus()
            for ident, scroll in [(p[0], self.stack.get_child_by_name(p[0])) for p in PAGES]:
                if w.is_ancestor(scroll):
                    self.navigate(ident)
                    break
            self.status.set_text(f"未保存：{errors[key]}（{key}）")
            return False
        try:
            replacement_key = self.draft.refine_key
            effect, restarted, _ = self.draft.persist(
                self.app.config_path,
                apply_now=True,
                status=str(self.app.state.status),
                restart_callback=lambda: self.app.root.after(0, self.app.backend.restart),
            )
        except ValueError as exc:
            self.status.set_text("未保存：" + str(exc))
            return False
        except Exception:
            self.status.set_text("保存失败，原配置保留。请检查文件写入权限。")
            return False
        self.app._invalidate_config_cache()
        self.app._update_tray_menu()
        if replacement_key:
            self.saved_refine_key = replacement_key
            self.controls["refine_api_key"][1].set_placeholder_text("留空保留已保存 Key")
        self.sync()
        self.status.set_text(
            "已保存，正在请求后端重启" if restarted else effect_status_message(effect, restarted=False)
        )
        return True

    def cancel_changes(self):
        self.draft.cancel()
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
            self.invalidate_discovery()
            self.sync()

    def close_request(self, *_):
        if not self.draft.dirty:
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


def open_settings_gtk(app, *, current, **_legacy):
    """Schedule creation or raise the existing settings window on the GTK loop."""

    def show():
        existing = getattr(app, "_gtk_settings_window", None)
        if existing is not None:
            existing.present()
            return False
        ui = NativeSettingsWindow(app, current)
        app._native_settings = ui
        app._gtk_settings_window = ui.window
        ui.window.present()
        return False

    app._glib.idle_add(show)
