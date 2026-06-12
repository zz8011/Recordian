from __future__ import annotations

import logging
import threading
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from recordian.config import ConfigManager
from recordian.setting_effects import effect_status_message
from recordian.tray_utils import save_config_changes

if TYPE_CHECKING:
    from recordian.tray_app import TrayApp

logger = logging.getLogger(__name__)


def open_speaker_enrollment_wizard(app: TrayApp) -> None:
    """Open speaker enrollment wizard for multi-sample registration."""
    if app._gtk is None or app._glib is None:
        return

    Gtk = app._gtk

    def _on_gtk_thread() -> bool:
        dialog = Gtk.Dialog(title="声纹注册向导", transient_for=None, flags=0)
        dialog.set_modal(True)
        dialog.set_default_size(650, 500)
        dialog.set_keep_above(True)
        content = dialog.get_content_area()
        content.set_border_width(12)

        # State
        wizard_state: dict[str, object] = {
            "step": 0,  # 0=intro, 1-3=recording samples, 4=complete
            "samples": [],  # List of numpy arrays
            "recording": False,
            "record_thread": None,
            "stop_event": None,
            "chunks": [],
            "record_handler_id": None,  # Signal handler ID
        }

        # UI elements
        title_label = Gtk.Label()
        title_label.set_markup("<big><b>声纹注册向导</b></big>")
        content.pack_start(title_label, False, False, 8)

        instruction_label = Gtk.Label()
        instruction_label.set_line_wrap(True)
        instruction_label.set_xalign(0.0)
        content.pack_start(instruction_label, False, False, 8)

        # Reference text area (hidden initially)
        reference_frame = Gtk.Frame(label="参考文本")
        scroller = Gtk.ScrolledWindow()
        scroller.set_hexpand(True)
        scroller.set_vexpand(True)
        scroller.set_min_content_height(120)
        reference_text_view = Gtk.TextView()
        reference_text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        reference_text_view.set_editable(False)
        reference_text_view.set_cursor_visible(False)
        reference_text_view.set_left_margin(8)
        reference_text_view.set_right_margin(8)
        scroller.add(reference_text_view)
        reference_frame.add(scroller)
        content.pack_start(reference_frame, True, True, 8)

        status_label = Gtk.Label()
        status_label.set_xalign(0.0)
        status_label.set_opacity(0.78)
        content.pack_start(status_label, False, False, 4)

        # Progress bar
        progress_label = Gtk.Label()
        progress_label.set_xalign(0.0)
        content.pack_start(progress_label, False, False, 4)

        # Buttons
        button_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_record = Gtk.Button(label="开始录制")
        btn_next = Gtk.Button(label="下一步")
        btn_next.set_sensitive(False)
        btn_cancel = Gtk.Button(label="取消")
        button_box.pack_start(btn_record, False, False, 0)
        button_box.pack_start(btn_next, False, False, 0)
        button_box.pack_end(btn_cancel, False, False, 0)
        content.pack_start(button_box, False, False, 8)

        def _update_ui() -> None:
            step = cast(int, wizard_state.get("step", 0))
            samples = wizard_state.get("samples", [])

            # Reference texts for each sample - designed to capture different voice characteristics
            reference_texts = [
                # Sample 1: Natural conversational tone, mixed Chinese/English
                "你好，我是这台电脑的主人。今天天气不错，我正在测试 Recordian 的声纹识别功能。\n"
                "I use voice input every day for coding and writing. "
                "希望系统能够准确识别我的声音，即使在有背景噪音的环境中。",

                # Sample 2: Clear enunciation, technical content, numbers
                "现在是第二段录音。请将识别结果发送到光标位置，保持自然的语速和停顿。\n"
                "我的工作涉及编程、文档编写和邮件回复。常用的编程语言包括 Python、JavaScript 和 TypeScript。\n"
                "今天的日期是 2025 年，版本号是 3.14.159。",

                # Sample 3: Varied pitch and emotion, questions and statements
                "这是最后一段测试录音。声纹识别技术真的很神奇！它能区分不同人的声音特征吗？\n"
                "当然可以。通过分析音色、音调、语速和发音习惯，系统可以建立独特的声纹模型。\n"
                "完成注册后，只有我的声音才能激活语音输入功能。"
            ]

            if step == 0:
                instruction_label.set_text(
                    "欢迎使用声纹注册向导！\n\n"
                    "您需要录制3个语音样本来创建声纹档案。\n"
                    "每个样本建议录制5-10秒，请在安静环境中清晰朗读参考文本。\n\n"
                    "点击「下一步」开始。"
                )
                reference_frame.hide()
                status_label.set_text("准备就绪")
                progress_label.set_text("进度: 0/3 样本")
                btn_record.set_visible(False)
                btn_next.set_label("下一步")
                btn_next.set_sensitive(True)
            elif 1 <= step <= 3:
                instruction_label.set_text(
                    f"样本 {step}/3\n\n"
                    "请按照下方参考文本朗读并录制。\n"
                    "点击「开始录制」，朗读完成后点击「停止录制」。"
                )
                # Show reference text
                reference_text_view.get_buffer().set_text(reference_texts[step - 1])
                reference_frame.show()
                reference_frame.show_all()
                status_label.set_text("等待录制")
                progress_label.set_text(f"进度: {len(cast(list, samples))}/3 样本")
                btn_record.set_visible(True)
                btn_record.set_label("开始录制")
                btn_record.set_sensitive(not wizard_state.get("recording", False))
                btn_next.set_label("下一步")
                btn_next.set_sensitive(len(cast(list, samples)) >= step)
            elif step == 4:
                instruction_label.set_text(
                    "注册完成！\n\n"
                    "已成功录制3个样本并创建声纹档案。\n"
                    "声纹验证功能已自动启用。"
                )
                reference_frame.hide()
                status_label.set_text("注册成功")
                progress_label.set_text("进度: 3/3 样本 ✓")
                btn_record.set_visible(False)
                btn_next.set_label("完成")
                btn_next.set_sensitive(True)

        def _start_recording(*_args: object) -> None:
            if wizard_state.get("recording"):
                return

            config = ConfigManager.load(app.config_path)
            sample_rate = int(config.get("wake_sample_rate", 16000))
            sample_rate = max(8000, min(48000, sample_rate))

            stop_event = threading.Event()
            chunks: list[object] = []
            wizard_state["stop_event"] = stop_event
            wizard_state["chunks"] = chunks
            wizard_state["recording"] = True

            def _worker() -> None:
                try:
                    import numpy as np
                    import sounddevice as sd

                    with sd.InputStream(
                        channels=1,
                        samplerate=sample_rate,
                        dtype="float32",
                        blocksize=1024,
                    ) as stream:
                        while not stop_event.is_set():
                            data, _ = stream.read(1024)
                            frame = np.ascontiguousarray(data.reshape(-1), dtype=np.float32)
                            chunks.append(frame.copy())
                except Exception as exc:  # noqa: BLE001
                    wizard_state["error"] = f"{type(exc).__name__}: {exc}"

            thread = threading.Thread(target=_worker, daemon=True)
            wizard_state["record_thread"] = thread
            thread.start()

            btn_record.set_label("停止录制")
            btn_record.set_sensitive(True)
            status_label.set_text("录制中... 请清晰朗读")

            def _on_record_click(*_args: object) -> None:
                _stop_recording()

            # Disconnect old handler
            handler_id = wizard_state.get("record_handler_id")
            if handler_id is not None:
                btn_record.disconnect(handler_id)

            # Connect new handler
            wizard_state["record_handler_id"] = btn_record.connect("clicked", _on_record_click)

        def _stop_recording() -> None:
            stop_event = wizard_state.get("stop_event")
            if isinstance(stop_event, threading.Event):
                stop_event.set()
            thread = wizard_state.get("record_thread")
            if isinstance(thread, threading.Thread):
                thread.join(timeout=3.0)
            wizard_state["recording"] = False
            wizard_state["record_thread"] = None

            # Process recorded audio
            chunks_obj = wizard_state.get("chunks", [])
            if not chunks_obj:
                status_label.set_text("未采集到音频，请重试")
                btn_record.set_label("开始录制")
                # Reconnect start handler
                handler_id = wizard_state.get("record_handler_id")
                if handler_id is not None:
                    btn_record.disconnect(handler_id)
                wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                return

            try:
                import numpy as np
                samples = np.concatenate(cast(list, chunks_obj)).astype(np.float32)

                if samples.size < 16000:  # Less than 1 second at 16kHz
                    status_label.set_text("录音太短，请至少录制1秒")
                    btn_record.set_label("开始录制")
                    # Reconnect start handler
                    handler_id = wizard_state.get("record_handler_id")
                    if handler_id is not None:
                        btn_record.disconnect(handler_id)
                    wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                    return

                # Quality check using speaker_verify
                try:
                    from recordian.speaker_verify import _assess_sample_quality
                    config = ConfigManager.load(app.config_path)
                    sample_rate = int(config.get("wake_sample_rate", 16000))
                    quality_ok, reason = _assess_sample_quality(samples, sample_rate)

                    if not quality_ok:
                        status_label.set_text(f"样本质量不足: {reason}，请重新录制")
                        btn_record.set_label("开始录制")
                        # Reconnect start handler
                        handler_id = wizard_state.get("record_handler_id")
                        if handler_id is not None:
                            btn_record.disconnect(handler_id)
                        wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                        return
                except Exception:  # noqa: BLE001
                    pass  # Skip quality check if not available

                # Save sample
                samples_list = wizard_state.get("samples")
                if not isinstance(samples_list, list):
                    samples_list = []
                    wizard_state["samples"] = samples_list
                samples_list.append(samples)
                step = cast(int, wizard_state.get("step", 0))
                status_label.set_text(f"样本 {step} 录制成功 ✓")
                btn_record.set_label("开始录制")
                # Reconnect start handler
                handler_id = wizard_state.get("record_handler_id")
                if handler_id is not None:
                    btn_record.disconnect(handler_id)
                wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
                _update_ui()

            except Exception as exc:  # noqa: BLE001
                status_label.set_text(f"处理失败: {type(exc).__name__}: {exc}")
                btn_record.set_label("开始录制")
                # Reconnect start handler
                handler_id = wizard_state.get("record_handler_id")
                if handler_id is not None:
                    btn_record.disconnect(handler_id)
                wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)

        def _next_step(*_args: object) -> None:
            step = cast(int, wizard_state.get("step", 0))

            if step == 4:
                # Complete
                dialog.destroy()
                return

            wizard_state["step"] = step + 1
            wizard_state["chunks"] = []

            if wizard_state["step"] == 4:
                # Save profile
                _save_profile()

            _update_ui()

        def _save_profile() -> None:
            try:
                import numpy as np

                from recordian.speaker_verify import enroll_speaker_profile  # type: ignore[attr-defined]

                config = ConfigManager.load(app.config_path)
                sample_rate = int(config.get("wake_sample_rate", 16000))
                profile_path = Path(config.get("wake_owner_profile", "~/.config/recordian/owner_voice_profile.json")).expanduser()

                samples = wizard_state.get("samples", [])
                if len(cast(list, samples)) < 3:
                    status_label.set_text("样本数量不足")
                    return

                # Save samples as WAV files
                sample_paths = []
                for i, sample in enumerate(cast(list, samples)):
                    sample_path = profile_path.parent / f"owner_sample_{i+1}.wav"
                    sample_path.parent.mkdir(parents=True, exist_ok=True)

                    pcm = (np.clip(sample, -1.0, 1.0) * 32767.0).astype("<i2")
                    with wave.open(str(sample_path), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)
                        wf.setframerate(sample_rate)
                        wf.writeframes(pcm.tobytes())
                    sample_paths.append(sample_path)

                # Create profile
                enroll_speaker_profile(
                    sample_paths=sample_paths,
                    profile_path=profile_path,
                    target_rate=sample_rate,
                )

                effect, restarted, _ = save_config_changes(
                    app.config_path,
                    {
                        "wake_owner_verify": True,
                        "wake_owner_profile": str(profile_path),
                    },
                    apply_now=True,
                    restart_callback=lambda: app.root.after(0, app.backend.restart),
                )
                status_label.set_text(
                    f"声纹档案已保存: {profile_path}；{effect_status_message(effect, restarted=restarted)}"
                )

            except Exception as exc:  # noqa: BLE001
                status_label.set_text(f"保存失败: {type(exc).__name__}: {exc}")

        def _cancel(*_args: object) -> None:
            if wizard_state.get("recording"):
                _stop_recording()
            dialog.destroy()

        wizard_state["record_handler_id"] = btn_record.connect("clicked", _start_recording)
        btn_next.connect("clicked", _next_step)
        btn_cancel.connect("clicked", _cancel)
        dialog.connect("delete-event", lambda *_: (_cancel(), False)[1])  # type: ignore[arg-type,func-returns-value]

        _update_ui()
        dialog.show_all()
        return False

    app._glib.idle_add(_on_gtk_thread)


__all__ = ["open_speaker_enrollment_wizard"]
