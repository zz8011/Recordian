"""Recordian hotkey-driven dictation daemon.

This module serves as the **entry-point** for ``python -m recordian.hotkey_dictate``.
All heavy logic has been decomposed into sibling modules:

* :mod:`arg_parser` — CLI / config argument parsing
* :mod:`recording_controller` — hotkey handler factories (oneshot / PTT / toggle)
* :mod:`realtime_asr` — streaming ASR worker & commit accumulator
* :mod:`state_manager` — recording state dataclass & thread-safe manager
* :mod:`text_cleanup` — partial-text delta helpers & final normalisation
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Re-exports for backward compatibility (e.g. tray_gui imports build_parser)
# ---------------------------------------------------------------------------
from .arg_parser import (  # noqa: F401
    build_parser,
    parse_hotkey_spec,
    _expand_key_name,
    _key_to_names,
    _parse_args_with_config,
    _save_runtime_config,
)
from .recording_controller import (  # noqa: F401
    build_hotkey_handlers,
    build_ptt_hotkey_handlers,
    _commit_text,
)
from .arg_parser import DEFAULT_CONFIG_PATH  # noqa: F401
from .linux_dictate import run_dictate_once  # noqa: F401
from .realtime_asr import (  # noqa: F401
    _RealtimeASRWorkerHandle,
    _start_realtime_asr_worker,
)
from .text_cleanup import (  # noqa: F401
    _normalize_final_text,
    _optimistic_first_partial,
    _stable_prefix_delta,
)
from .state_manager import RecordingState  # noqa: F401
from .postprocess_pipeline import (  # noqa: F401
    _apply_refine_postprocess,
    _apply_target_window,
    _build_refine_prompt_with_protected_terms,
    _cleanup_repeat_lite_text,
    _cleanup_stutter_text,
    _coerce_bool,
    _extract_refine_postprocess_rule,
    _resolve_auto_hard_enter,
    _select_refine_protected_terms,
    _should_skip_owner_gated_asr,
    _text_contains_term,
)
from .wake_session_monitor import (  # noqa: F401
    _adaptive_vad_threshold,
    _display_audio_level,
    _float_to_pcm16le,
    _is_level_speech_frame,
    _is_soft_keepalive_speech_frame,
    _owner_gate_level,
    _pcm16le_to_f32,
    _pick_vad_sample_rate,
    _resample_audio_for_vad,
    _semantic_text_has_content,
    _semantic_text_signal_len,
    _should_auto_stop_semantic_session,
    _update_speech_evidence,
    _vad_frame_bytes,
)

__all__ = [
    "build_parser",
    "build_hotkey_handlers",
    "build_ptt_hotkey_handlers",
    "main",
    "parse_hotkey_spec",
    "RecordingState",
]


# ===================================================================
# main() — CLI entry point
# ===================================================================

def main() -> None:
    import logging
    import sys

    from recordian.error_tracker import get_error_tracker

    logger = logging.getLogger(__name__)

    def handle_exception(exc_type, exc_value, exc_traceback):
        """Global exception handler."""
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc_value, exc_traceback)
            return

        logger.error("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(exc_value)

    sys.excepthook = handle_exception

    try:
        _main_impl()
    except Exception as e:
        logger.error(f"Fatal error in main: {e}", exc_info=True)
        tracker = get_error_tracker()
        if tracker:
            tracker.capture_exception(e)
        raise


def _main_impl() -> None:
    """Main implementation."""
    import json
    import threading
    import time
    from pathlib import Path

    from recordian.notify import Notification, resolve_notifier
    from recordian.voice_wake import (
        VoiceWakeService,
        make_wake_model_config,
        make_wake_runtime_config,
    )

    parser = build_parser()
    args = _parse_args_with_config(parser)
    if args.save_config:
        _save_runtime_config(args)

    try:
        from pynput import keyboard
    except ModuleNotFoundError as exc:
        raise RuntimeError("pynput not installed. Run: pip install -e '.[hotkey]'") from exc

    def _print_json(payload: dict[str, object]) -> None:
        print(json.dumps(payload, ensure_ascii=False), flush=True)

    notifier = resolve_notifier(args.notify_backend)

    def _notify(payload: dict[str, object]) -> None:
        event = str(payload.get("event", ""))
        if event == "ready":
            notifier.notify(
                Notification(
                    title="Recordian 已就绪",
                    body=f"触发: {args.hotkey} 退出: {args.exit_hotkey or '禁用'}",
                    urgency="low",
                )
            )
            return
        if event == "recording_started":
            notifier.notify(Notification(title="Recordian", body="开始录音", urgency="low"))
            return
        if event == "busy":
            notifier.notify(Notification(title="Recordian", body="仍在处理上一条语音", urgency="low"))
            return
        if event == "error":
            detail = str(payload.get("error", "unknown_error"))
            notifier.notify(Notification(title="Recordian 错误", body=detail, urgency="critical"))
            return
        if event == "result":
            result = payload.get("result")
            text = ""
            if isinstance(result, dict):
                text = str(result.get("text", "")).strip()
            body = _truncate_text(text, max_len=40) if text else "识别为空"
            notifier.notify(Notification(title="Recordian 识别完成", body=body, urgency="normal"))
            return
        if event == "stopped":
            notifier.notify(Notification(title="Recordian 已退出", body="热键守护进程已停止", urgency="low"))

    def _emit(payload: dict[str, object]) -> None:
        _print_json(payload)
        try:
            _notify(payload)
        except Exception:
            # Notification failure should not break dictation flow.
            pass

    trigger_keys = parse_hotkey_spec(args.hotkey)
    stop_keys = parse_hotkey_spec(args.stop_hotkey) if getattr(args, "stop_hotkey", "").strip() else set()
    toggle_keys = parse_hotkey_spec(args.toggle_hotkey) if getattr(args, "toggle_hotkey", "").strip() else set()
    exit_keys = parse_hotkey_spec(args.exit_hotkey) if args.exit_hotkey.strip() else set()
    if not trigger_keys:
        raise RuntimeError("empty hotkey is not allowed")

    voice_wake_service: VoiceWakeService | None = None

    if args.trigger_mode in {"ptt", "toggle"}:
        start_recording, stop_recording, exit_daemon, stop_event = build_ptt_hotkey_handlers(
            args=args,
            on_result=_emit,
            on_error=_emit,
            on_busy=_emit,
            on_state=_emit,
        )

        def _request_stop_recording() -> None:
            threading.Thread(target=stop_recording, daemon=True, name="recordian-stop-recording").start()

        if bool(getattr(args, "enable_voice_wake", False)):
            runtime_cfg = make_wake_runtime_config(args)
            model_cfg = make_wake_model_config(args)

            def _on_wake(keyword: str) -> None:
                try:
                    start_recording("voice_wake")
                except Exception as exc:  # noqa: BLE001
                    _emit({"event": "error", "error": f"voice_wake_start_failed: {exc}"})

            voice_wake_service = VoiceWakeService(
                model=model_cfg,
                runtime=runtime_cfg,
                on_wake=_on_wake,
                on_event=_emit,
                cache_dir=Path.home() / ".cache" / "recordian" / "wake",
            )
            voice_wake_service.start()
        trigger_pressed = {"active": False}

        pressed: set[str] = set()

        if args.trigger_mode == "ptt":
            toggle_recording = {"active": False}
            toggle_pressed = {"active": False}
            stop_pressed = {"active": False}

            def _on_press(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.update(key_names)
                if exit_keys and exit_keys.issubset(pressed):
                    exit_daemon()
                    return False

                # Toggle stop key — only intercept when toggle is active
                if stop_keys and stop_keys.issubset(pressed) and not stop_pressed["active"] and toggle_recording["active"]:
                    stop_pressed["active"] = True
                    toggle_recording["active"] = False
                    _request_stop_recording()
                    return True

                # Toggle start key (only when not a subset of PTT key to avoid double-trigger)
                if toggle_keys and toggle_keys.issubset(pressed) and not toggle_pressed["active"]:
                    toggle_pressed["active"] = True
                    if not toggle_recording["active"]:
                        try:
                            toggle_recording["active"] = start_recording()
                        except Exception as exc:  # noqa: BLE001
                            toggle_recording["active"] = False
                            _emit({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
                    return True

                # PTT: only when toggle is not active
                if trigger_keys.issubset(pressed) and not trigger_pressed["active"] and not toggle_recording["active"]:
                    trigger_pressed["active"] = True
                    try:
                        start_recording()
                    except Exception as exc:  # noqa: BLE001
                        _emit({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
                return True

            def _on_release(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.difference_update(key_names)
                if toggle_pressed["active"] and not (toggle_keys and toggle_keys.issubset(pressed)):
                    toggle_pressed["active"] = False
                if stop_pressed["active"] and not (stop_keys and stop_keys.issubset(pressed)):
                    stop_pressed["active"] = False
                # PTT release: stop only if PTT was active (not toggle)
                if trigger_pressed["active"] and not trigger_keys.issubset(pressed):
                    trigger_pressed["active"] = False
                    if not toggle_recording["active"]:
                        _request_stop_recording()
                if stop_event.is_set():
                    return False
                return True
        else:
            # toggle: start and stop can be different keys
            recording = {"active": False}
            stop_trigger_pressed = {"active": False}

            def _on_press(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.update(key_names)
                if exit_keys and exit_keys.issubset(pressed):
                    exit_daemon()
                    return False
                # Dedicated stop key
                if stop_keys and stop_keys.issubset(pressed) and not stop_trigger_pressed["active"]:
                    stop_trigger_pressed["active"] = True
                    if recording["active"]:
                        recording["active"] = False
                        _request_stop_recording()
                    return True
                # Start key
                if trigger_keys.issubset(pressed) and not trigger_pressed["active"]:
                    trigger_pressed["active"] = True
                    if not recording["active"]:
                        try:
                            recording["active"] = start_recording()
                        except Exception as exc:  # noqa: BLE001
                            recording["active"] = False
                            _emit({"event": "error", "error": f"{type(exc).__name__}: {exc}"})
                    elif not stop_keys:
                        # No dedicated stop key: same key toggles off
                        recording["active"] = False
                        _request_stop_recording()
                return True

            def _on_release(key: object):
                key_names = _key_to_names(key, keyboard)
                if not key_names:
                    return True
                pressed.difference_update(key_names)
                if trigger_pressed["active"] and not trigger_keys.issubset(pressed):
                    trigger_pressed["active"] = False
                if stop_trigger_pressed["active"] and not (stop_keys and stop_keys.issubset(pressed)):
                    stop_trigger_pressed["active"] = False
                if stop_event.is_set():
                    return False
                return True
    else:
        run_once, exit_daemon, stop_event = build_hotkey_handlers(
            args=args,
            on_result=_emit,
            on_error=_emit,
            on_busy=_emit,
        )
        if bool(getattr(args, "enable_voice_wake", False)):
            _emit({"event": "log", "message": "trigger_mode=oneshot 时不支持语音唤醒，已忽略"})
        trigger_pressed = {"active": False}

        pressed: set[str] = set()

        def _on_press(key: object):
            key_names = _key_to_names(key, keyboard)
            if not key_names:
                return True
            pressed.update(key_names)
            if exit_keys and exit_keys.issubset(pressed):
                exit_daemon()
                return False
            if trigger_keys.issubset(pressed) and not trigger_pressed["active"]:
                trigger_pressed["active"] = True
                run_once()
            return True

        def _on_release(key: object):
            key_names = _key_to_names(key, keyboard)
            if not key_names:
                return True
            pressed.difference_update(key_names)
            if trigger_pressed["active"] and not trigger_keys.issubset(pressed):
                trigger_pressed["active"] = False
            if stop_event.is_set():
                return False
            return True

    _emit(
        {
            "event": "ready",
            "hotkey": args.hotkey,
            "exit_hotkey": args.exit_hotkey,
            "cooldown_ms": args.cooldown_ms,
            "trigger_mode": args.trigger_mode,
            "config_path": args.config_path,
            "notify_backend": notifier.backend_name,
            "voice_wake_enabled": bool(getattr(args, "enable_voice_wake", False)),
        }
    )

    with keyboard.Listener(on_press=_on_press, on_release=_on_release) as listener:
        while not stop_event.is_set():
            time.sleep(0.1)
        listener.stop()

    if voice_wake_service is not None:
        voice_wake_service.stop()

    _emit({"event": "stopped"})


def _truncate_text(text: str, *, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _merge_stream_text(prev: str, current: str) -> str:
    """合并流式 ASR 文本：若 current 以 prev 开头则直接返回 current，否则拼接。"""
    if not prev:
        return current
    if current.startswith(prev):
        return current
    if prev.endswith(current):
        return prev
    return prev + current


if __name__ == "__main__":
    main()
