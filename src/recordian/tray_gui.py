from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk
from typing import Any


DEFAULT_CONFIG_PATH = "~/.config/recordian/hotkey.json"


def load_svg_as_image(svg_path: Path, size: tuple[int, int] = (64, 64)):
    """Load SVG file and convert to PIL Image."""
    try:
        from PIL import Image
        import cairosvg
        import io

        png_data = cairosvg.svg2png(url=str(svg_path), output_width=size[0], output_height=size[1])
        return Image.open(io.BytesIO(png_data))
    except ImportError:
        # Fallback: use PIL to create a simple colored circle
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((5, 5, size[0]-5, size[1]-5), fill=(110, 231, 183, 255))
        return img
    except Exception:
        # Fallback to simple circle
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", size, (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((5, 5, size[0]-5, size[1]-5), fill=(110, 231, 183, 255))
        return img


def parse_backend_event_line(line: str) -> dict[str, object] | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if isinstance(obj, dict) and "event" in obj:
        return obj
    return None


def load_runtime_config(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def save_runtime_config(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recordian tray GUI with waveform overlay.")
    parser.add_argument("--config-path", default=DEFAULT_CONFIG_PATH)
    parser.add_argument("--no-auto-start", action="store_true")
    parser.add_argument("--notify-backend", choices=["none", "auto", "notify-send", "stdout"], default="auto")
    return parser


@dataclass(slots=True)
class UiState:
    status: str = "idle"
    detail: str = "Idle"
    last_text: str = ""
    backend_running: bool = False
    last_record_ms: float = 0.0
    last_transcribe_ms: float = 0.0
    last_refine_ms: float = 0.0
    last_total_ms: float = 0.0


def get_logo_path(status: str) -> Path:
    """Get logo path based on current status."""
    # Get project root (assuming tray_gui.py is in src/recordian/)
    project_root = Path(__file__).parent.parent.parent
    assets_dir = project_root / "assets"

    logo_map = {
        "idle": "logo.svg",
        "recording": "logo-recording.svg",
        "processing": "logo-recording.svg",
        "error": "logo-error.svg",
        "stopped": "logo.svg",
        "starting": "logo-warming.svg",
        "warming": "logo-warming.svg",
        "busy": "logo-warming.svg",
    }

    logo_file = logo_map.get(status, "logo.svg")
    logo_path = assets_dir / logo_file

    if not logo_path.exists():
        # Fallback to default logo
        logo_path = assets_dir / "logo.svg"

    return logo_path


class WaveOverlay:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = "idle"
        self.amplitude = 0.0
        self.target_amplitude = 0.0
        self.level_boost = 0.0
        self.base_mode = 0.0
        self.detail = ""
        self.hide_deadline: float | None = None
        self.smooth_audio = 0.0  # Smoothed audio level for scaling
        self._cmd_queue: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._init_error: Exception | None = None
        self._thread = threading.Thread(target=self._run_shader_loop, daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)
        if self._init_error is not None:
            raise RuntimeError(f"shader overlay init failed: {self._init_error}") from self._init_error

    def _run_shader_loop(self) -> None:
        try:
            import pyglet
            from pyglet import gl
            from pyglet.graphics.shader import Shader, ShaderProgram
        except Exception as exc:  # noqa: BLE001
            self._init_error = exc
            self._ready.set()
            return

        vertex_src = """
#version 330 core
in vec2 position;
out vec2 v_uv;
void main() {
    v_uv = position * 0.5 + 0.5;
    gl_Position = vec4(position, 0.0, 1.0);
}
"""
        fragment_src = """
#version 330 core
in vec2 v_uv;
out vec4 fragColor;
uniform vec2 u_resolution;
uniform float u_audio;
uniform float u_motion;
uniform float u_time;

float SoftEllipse(vec2 uv, float width, float height, float blur) {
    float d = length(uv / vec2(width, height));
    return smoothstep(1.0, 1.0 - blur, d);
}

void main() {
    vec2 uv = (v_uv - 0.5) * 2.0;
    uv.x *= u_resolution.x / u_resolution.y;

    // Simulate audio volume
    float vol = u_audio * u_motion;

    // Sphere projection distortion
    float len = length(uv * 1.8);
    vec2 distortedUV = uv;
    if(len < 1.0) {
        float as = tan(asin(len));
        distortedUV *= as * 0.4;
    }

    vec3 finalColor = vec3(0.0);

    // Define four vibrant colors
    vec3 c1 = vec3(0.1, 0.5, 1.0);  // Bright blue
    vec3 c2 = vec3(0.8, 0.2, 0.9);  // Violet
    vec3 c3 = vec3(0.1, 0.9, 0.7);  // Aurora green
    vec3 c4 = vec3(1.0, 0.4, 0.4);  // Coral red

    // Draw multiple dynamic ellipse layers
    for(int i = 0; i < 4; i++) {
        float fi = float(i);

        // Base time always moves forward
        float baseTime = u_time * (0.8 + fi * 0.2);

        // Volume adds extra rotation speed (always positive, never subtracts)
        float speedBoost = vol * 5.0;  // Linear response, stronger multiplier
        float t = baseTime + u_time * speedBoost;

        // Calculate irregular motion trajectory - FIXED amplitude
        vec2 offset = vec2(
            sin(t + fi * 1.5) * 0.18,  // Fixed amplitude
            cos(t * 0.7 + fi * 2.0) * 0.12  // Fixed amplitude
        );

        // Size changes slightly with volume
        float size = 0.28 + vol * 0.5 + sin(t * 0.5) * 0.06;
        float mask = SoftEllipse(distortedUV + offset, size, size * 0.7, 0.8);

        // Color blending
        vec3 col = c1;
        if(i==1) col = c2;
        if(i==2) col = c3;
        if(i==3) col = c4;

        finalColor += col * mask * 0.7;
    }

    // Core highlight - much softer and less visible
    float core = SoftEllipse(distortedUV, 0.08 + vol * 0.08, 0.04, 0.95);
    finalColor += vec3(1.0, 1.0, 1.0) * core * 0.2;  // Reduced from 0.5 to 0.2

    // Background glow
    vec3 bg = vec3(0.02, 0.03, 0.08) * (1.0 - length(uv));

    // Create circular mask
    float distFromCenter = length(uv);
    float scale = 1.0 + vol * 0.5;
    float circularMask = smoothstep(0.55 / scale, 0.50 / scale, distFromCenter);

    fragColor = vec4(finalColor + bg, circularMask * 0.95);
}
"""
        try:
            config = gl.Config(double_buffer=True, alpha_size=8)
            overlay_style = getattr(
                pyglet.window.Window,
                "WINDOW_STYLE_OVERLAY",
                pyglet.window.Window.WINDOW_STYLE_BORDERLESS,
            )
            window = pyglet.window.Window(
                width=352,
                height=352,
                caption="Recordian Overlay",
                style=overlay_style,
                resizable=False,
                visible=False,
                config=config,
            )
        except Exception:
            overlay_style = getattr(
                pyglet.window.Window,
                "WINDOW_STYLE_OVERLAY",
                pyglet.window.Window.WINDOW_STYLE_BORDERLESS,
            )
            window = pyglet.window.Window(
                width=352,
                height=352,
                caption="Recordian Overlay",
                style=overlay_style,
                resizable=False,
                visible=False,
            )
        window.set_vsync(False)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        try:
            # Keep overlay visible but non-interactive to avoid stealing input focus.
            window.set_mouse_passthrough(True)
        except Exception:
            pass

        # Find the screen containing the mouse cursor for multi-monitor support
        display = window.display
        try:
            # Get mouse position using X11
            import ctypes
            from pyglet.libs.x11 import xlib

            x_display = getattr(window, "_x_display", None)
            if x_display is not None:
                root = xlib.XDefaultRootWindow(x_display)
                root_return = xlib.Window()
                child_return = xlib.Window()
                root_x = ctypes.c_int()
                root_y = ctypes.c_int()
                win_x = ctypes.c_int()
                win_y = ctypes.c_int()
                mask = ctypes.c_uint()

                xlib.XQueryPointer(
                    x_display, root,
                    ctypes.byref(root_return), ctypes.byref(child_return),
                    ctypes.byref(root_x), ctypes.byref(root_y),
                    ctypes.byref(win_x), ctypes.byref(win_y),
                    ctypes.byref(mask)
                )

                mouse_x, mouse_y = root_x.value, root_y.value

                # Find which screen contains the mouse
                target_screen = None
                for screen in display.get_screens():
                    if (screen.x <= mouse_x < screen.x + screen.width and
                        screen.y <= mouse_y < screen.y + screen.height):
                        target_screen = screen
                        break

                # Fallback to default screen if mouse position detection fails
                if target_screen is None:
                    target_screen = display.get_default_screen()
            else:
                target_screen = display.get_default_screen()
        except Exception:
            # Fallback to default screen on any error
            target_screen = display.get_default_screen()

        # Calculate position relative to the target screen
        _pos_x = max(0, target_screen.x + (target_screen.width - window.width) // 2)
        # set_location uses top-left screen coords (y=0 is top).
        # Place window at bottom center of the target screen (with some margin).
        _pos_y = max(0, target_screen.y + target_screen.height - window.height - 80)

        try:
            from pyglet.libs.x11 import xlib as x11

            display = getattr(window, "_x_display", None)
            xwin = getattr(window, "_window", None)
            if display is not None and xwin is not None:
                # Disable input focus
                wm_hints = x11.XAllocWMHints()
                if wm_hints:
                    wm_hints.contents.flags = x11.InputHint
                    wm_hints.contents.input = 0
                    x11.XSetWMHints(display, xwin, wm_hints)
                    x11.XFree(wm_hints)

                # Set PPosition flag so WM respects our position on first map
                sz_hints = x11.XAllocSizeHints()
                if sz_hints:
                    sz_hints.contents.flags = x11.PPosition | x11.USPosition
                    sz_hints.contents.x = _pos_x
                    sz_hints.contents.y = _pos_y
                    x11.XSetWMNormalHints(display, xwin, sz_hints)
                    x11.XFree(sz_hints)

                x11.XFlush(display)
        except Exception:
            pass

        window.set_location(_pos_x, _pos_y)

        program = ShaderProgram(
            Shader(vertex_src, "vertex"),
            Shader(fragment_src, "fragment"),
        )
        quad = program.vertex_list(
            4,
            gl.GL_TRIANGLE_STRIP,
            position=("f", [-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0]),
        )
        # Label removed - no text display on overlay
        start_time = time.monotonic()
        phase = 0.0

        @window.event
        def on_show() -> None:
            # Window manager may ignore set_location before the window is mapped.
            # Re-apply position every time the window becomes visible.
            window.set_location(_pos_x, _pos_y)

        @window.event
        def on_draw() -> None:
            window.clear()
            program.use()
            program["u_resolution"] = (float(window.width), float(window.height))
            program["u_time"] = float(time.monotonic() - start_time + phase)
            # Use smoothed audio level for scaling
            program["u_audio"] = float(max(0.0, min(1.0, self.smooth_audio)))
            motion = 0.0
            if self.state == "recording":
                motion = max(0.0, min(1.0, (self.amplitude - 0.10) / 0.62))
            elif self.state == "processing":
                motion = 0.0  # Completely still in processing state
            program["u_motion"] = float(motion)
            quad.draw(gl.GL_TRIANGLE_STRIP)
            # Text label removed - no text display on overlay

        def update(dt: float) -> None:
            nonlocal phase
            phase += dt
            while True:
                try:
                    cmd, payload = self._cmd_queue.get_nowait()
                except queue.Empty:
                    break
                if cmd == "quit":
                    pyglet.app.exit()
                    return
                if cmd == "state":
                    state, detail = payload  # type: ignore[misc]
                    self.state = str(state)
                    self.detail = str(detail)
                    self.hide_deadline = None
                    if self.state == "recording":
                        self.target_amplitude = 0.0
                        self.level_boost = 0.0
                        self.amplitude = 0.0
                        self.base_mode = 1.0
                        window.set_location(_pos_x, _pos_y)
                        window.set_visible(True)
                    elif self.state == "processing":
                        # Processing: return to idle state (original size and color)
                        self.target_amplitude = 0.0
                        self.amplitude = 0.0
                        self.base_mode = 0.0
                        self.level_boost = 0.0
                        self.hide_deadline = time.time() + 0.50
                    elif self.state == "error":
                        self.target_amplitude = 0.50
                        self.base_mode = 3.0
                        window.set_location(_pos_x, _pos_y)
                        window.set_visible(True)
                        self.hide_deadline = time.time() + 1.55
                    else:
                        self.target_amplitude = 0.0
                        self.base_mode = 0.0
                        self.hide_deadline = time.time() + (1.1 if self.detail.strip() else 0.35)
                elif cmd == "level":
                    level = max(0.0, min(1.0, float(payload)))  # type: ignore[arg-type]
                    # Gate ambient noise: only pass through signal above threshold.
                    # Increased threshold to filter out more background noise
                    self.level_boost = max(0.0, level - 0.15)

            target = self.target_amplitude
            if self.state == "recording":
                target = min(1.0, target + self.level_boost * 0.6)
                self.level_boost *= 0.60  # faster decay for smoother animation
                # Smooth audio level for scaling - much faster response for real-time feel
                self.smooth_audio += (self.amplitude - self.smooth_audio) * 0.45  # Increased from 0.25
            elif self.state == "processing":
                # Keep still - no animation in processing state
                target = 0.0
                # Smooth decrease back to 0
                self.smooth_audio += (0.0 - self.smooth_audio) * 0.45
            else:
                # Other states: reset smoothly
                self.smooth_audio += (0.0 - self.smooth_audio) * 0.45
            self.amplitude += (target - self.amplitude) * 0.035  # increased from 0.018 for faster response

            if self.hide_deadline is not None and time.time() >= self.hide_deadline:
                window.set_visible(False)
                self.hide_deadline = None

        pyglet.clock.schedule_interval(update, 1.0 / 60.0)
        self._ready.set()
        pyglet.app.run()

    def set_state(self, state: str, detail: str = "") -> None:
        self._cmd_queue.put(("state", (state, detail)))

    def set_level(self, level: float) -> None:
        self._cmd_queue.put(("level", float(level)))

    def shutdown(self) -> None:
        self._cmd_queue.put(("quit", None))
        self._thread.join(timeout=1.5)


class TrayApp:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.config_path = Path(args.config_path).expanduser()
        self.state = UiState()
        self.events: queue.Queue[dict[str, object]] = queue.Queue()
        self._warmup_done = False

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Recordian Tray")

        self.overlay = WaveOverlay(self.root)
        self.icon = None
        self.indicator = None
        self._tray_thread: threading.Thread | None = None

        self.backend_proc: subprocess.Popen[str] | None = None
        self.backend_threads: list[threading.Thread] = []
        self._settings_window: tk.Toplevel | None = None

    def run(self) -> None:
        self._start_tray()
        if not self.args.no_auto_start:
            self.start_backend()
        self.root.after(80, self._poll_events)
        self.root.mainloop()

    def _poll_events(self) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self._handle_event(event)
        self.root.after(80, self._poll_events)

    def _handle_event(self, event: dict[str, object]) -> None:
        et = str(event.get("event", ""))
        if et == "ready":
            self.state.backend_running = True
            self.state.status = "idle"
            self.state.detail = "Ready"
            # Suppress overlay animation during startup warmup phase to
            # avoid the "flash twice on launch" issue (PRD 4.4).
            if self._warmup_done:
                self.overlay.set_state("idle", "Ready")
        elif et == "model_warmup":
            status = str(event.get("status", ""))
            if status == "starting":
                self.state.backend_running = True
                self.state.status = "warming"
                self.state.detail = "Model warmup..."
            elif status == "ready":
                self._warmup_done = True
                latency_ms = float(event.get("latency_ms", 0.0) or 0.0)
                self.state.status = "idle"
                self.state.detail = f"Warmup ready ({latency_ms:.0f}ms)"
        elif et == "recording_started":
            self.state.status = "recording"
            self.state.detail = "Recording..."
            self.overlay.set_state("recording", "Listening...")
        elif et == "stream_partial":
            text = str(event.get("text", "")).strip()
            if text:
                self.state.last_text = text
                # Keep one-shot UX: do not show streaming words while recording.
                if self.state.status == "recording":
                    self.state.detail = "Recording..."
        elif et == "audio_level":
            self.overlay.set_level(float(event.get("level", 0.0) or 0.0))
        elif et == "processing_started":
            self.state.status = "processing"
            self.state.detail = "Processing..."
            self.overlay.set_state("processing", "Recognizing...")
        elif et == "result":
            result = event.get("result")
            text = ""
            commit_info: dict[str, object] = {}
            if isinstance(result, dict):
                text = str(result.get("text", "")).strip()
                raw_commit = result.get("commit")
                if isinstance(raw_commit, dict):
                    commit_info = raw_commit
                # Extract performance metrics
                self.state.last_record_ms = float(result.get("record_latency_ms", 0.0) or 0.0)
                self.state.last_transcribe_ms = float(result.get("transcribe_latency_ms", 0.0) or 0.0)
                self.state.last_refine_ms = float(result.get("refine_latency_ms", 0.0) or 0.0)
                self.state.last_total_ms = self.state.last_record_ms + self.state.last_transcribe_ms + self.state.last_refine_ms
            self.state.last_text = text
            self.state.status = "idle"
            commit_backend = str(commit_info.get("backend", ""))
            commit_detail = str(commit_info.get("detail", ""))
            committed = bool(commit_info.get("committed", False))
            if text:
                print(
                    f"result text={text} committed={committed} backend={commit_backend} detail={commit_detail}",
                    file=sys.stderr,
                    flush=True,
                )
            else:
                print(
                    f"result text=<empty> committed={committed} backend={commit_backend} detail={commit_detail}",
                    file=sys.stderr,
                    flush=True,
                )
            if text:
                if committed:
                    self.state.detail = _truncate(text, 42)
                else:
                    detail = str(commit_info.get("detail", "not_committed"))
                    self.state.detail = _truncate(f"已识别(未上屏): {text}", 42)
                    self.events.put({"event": "log", "message": f"commit_failed: {detail}"})
                self.overlay.set_state("idle", _truncate(text, 48))
            else:
                self.state.detail = "识别为空"
                self.overlay.set_state("idle", "No speech detected")
        elif et == "busy":
            self.state.status = "busy"
            self.state.detail = "Busy"
            self.overlay.set_state("processing", "Still processing previous input")
        elif et == "error":
            self.state.status = "error"
            self.state.detail = str(event.get("error", "error"))
            self.overlay.set_state("error", _truncate(self.state.detail, 72))
        elif et in {"stopped", "backend_exited"}:
            self.state.backend_running = False
            self.state.status = "stopped"
            self.state.detail = "Stopped"
            self.overlay.set_state("idle", "Stopped")
        elif et == "log":
            msg = str(event.get("message", "")).strip()
            if msg:
                self.state.detail = _truncate(msg, 48)
                if msg.startswith("diag "):
                    print(msg, file=sys.stderr, flush=True)
        self._update_tray_menu()

    def _backend_cmd(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "recordian.hotkey_dictate",
            "--config-path",
            str(self.config_path),
            "--notify-backend",
            "none",
        ]

    def start_backend(self) -> None:
        if self.backend_proc is not None and self.backend_proc.poll() is None:
            return
        cmd = self._backend_cmd()
        self.backend_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.state.backend_running = True
        self.state.status = "starting"
        self.state.detail = "Starting backend..."
        self._update_tray_menu()

        assert self.backend_proc.stdout is not None
        assert self.backend_proc.stderr is not None
        t_out = threading.Thread(target=self._read_stream, args=(self.backend_proc.stdout, False), daemon=True)
        t_err = threading.Thread(target=self._read_stream, args=(self.backend_proc.stderr, True), daemon=True)
        t_wait = threading.Thread(target=self._wait_backend, daemon=True)
        self.backend_threads = [t_out, t_err, t_wait]
        for t in self.backend_threads:
            t.start()

    def stop_backend(self) -> None:
        proc = self.backend_proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2.0)
        self.backend_proc = None
        self.events.put({"event": "stopped"})

    def restart_backend(self) -> None:
        self.stop_backend()
        self.start_backend()

    def toggle_quick_mode(self, enabled: bool) -> None:
        """切换快速模式（跳过文字优化）"""
        config = load_runtime_config(self.config_path)
        config["enable_text_refine"] = not enabled  # enabled=True 表示快速模式，即不启用文字优化
        save_runtime_config(self.config_path, config)

        # 重启后端使配置生效
        if self.state.backend_running:
            self.restart_backend()
            mode_text = "快速模式" if enabled else "质量模式"
            self.events.put({"event": "log", "message": f"已切换到{mode_text}，后端已重启"})

    def copy_last_text(self) -> None:
        """复制最后识别的文本到剪贴板"""
        if not self.state.last_text:
            return
        try:
            # 使用 tkinter 剪贴板
            self.root.clipboard_clear()
            self.root.clipboard_append(self.state.last_text)
            self.root.update()
            self.events.put({"event": "log", "message": f"已复制: {self.state.last_text[:30]}..."})
        except Exception as e:
            self.events.put({"event": "log", "message": f"复制失败: {e}"})

    def switch_preset(self, preset_name: str) -> None:
        """切换文字优化 preset"""
        config = load_runtime_config(self.config_path)
        config["refine_preset"] = preset_name
        save_runtime_config(self.config_path, config)

        # 重启后端使配置生效
        if self.state.backend_running:
            self.restart_backend()
            self.events.put({"event": "log", "message": f"已切换到 {preset_name} preset，后端已重启"})


    def _read_stream(self, stream, is_stderr: bool) -> None:  # noqa: ANN001
        for raw in iter(stream.readline, ""):
            event = parse_backend_event_line(raw)
            if event is not None:
                self.events.put(event)
            elif is_stderr:
                text = raw.strip()
                if text:
                    self.events.put({"event": "log", "message": text})
        stream.close()

    def _wait_backend(self) -> None:
        proc = self.backend_proc
        if proc is None:
            return
        code = proc.wait()
        self.events.put({"event": "backend_exited", "code": code})

    def open_settings(self) -> None:
        if self._settings_window is not None and self._settings_window.winfo_exists():
            self._settings_window.lift()
            return

        current = load_runtime_config(self.config_path)
        win = tk.Toplevel(self.root)
        win.title("Recordian Settings")
        win.geometry("600x700")
        win.attributes("-topmost", True)
        self._settings_window = win

        # Create scrollable frame
        canvas = tk.Canvas(win)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        scrollable_frame = ttk.Frame(canvas)

        scrollable_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )

        canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        fields = [
            # 热键设置
            ("hotkey", "触发热键", current.get("hotkey", "<ctrl_r>")),
            ("stop_hotkey", "停止热键", current.get("stop_hotkey", "")),
            ("toggle_hotkey", "切换热键", current.get("toggle_hotkey", "")),
            ("exit_hotkey", "退出热键", current.get("exit_hotkey", "<ctrl>+<alt>+q")),
            ("trigger_mode", "触发模式 (ptt/toggle)", current.get("trigger_mode", "ptt")),
            ("cooldown_ms", "冷却时间 (ms)", str(current.get("cooldown_ms", 300))),

            # 录音设置
            ("duration", "录音时长 (s)", str(current.get("duration", 4.0))),
            ("record_backend", "录音后端 (auto/ffmpeg/arecord)", current.get("record_backend", "auto")),
            ("record_format", "录音格式 (ogg/wav/mp3)", current.get("record_format", "ogg")),
            ("sample_rate", "采样率", str(current.get("sample_rate", 16000))),
            ("channels", "声道数", str(current.get("channels", 1))),
            ("input_device", "输入设备", current.get("input_device", "default")),

            # ASR 设置
            ("asr_provider", "ASR Provider (qwen-asr)", current.get("asr_provider", "qwen-asr")),
            ("qwen_model", "Qwen ASR 模型路径", current.get("qwen_model", "")),
            ("qwen_language", "Qwen 语言 (Chinese/auto)", current.get("qwen_language", "Chinese")),
            ("qwen_max_new_tokens", "Qwen Max Tokens", str(current.get("qwen_max_new_tokens", 1024))),
            ("asr_context_preset", "ASR Context 预设 (留空/default/formal/meeting/technical/simple)", current.get("asr_context_preset", "")),
            ("asr_context", "ASR Context 自定义 (优先级低于预设)", current.get("asr_context", "")),
            ("device", "计算设备 (cpu/cuda/auto)", current.get("device", "cuda")),

            # 文本精炼设置
            ("enable_text_refine", "启用文本精炼", current.get("enable_text_refine", False)),
            ("refine_provider", "精炼 Provider (local/cloud)", current.get("refine_provider", "local")),
            ("refine_preset", "精炼预设", current.get("refine_preset", "default")),
            ("refine_model", "本地精炼模型路径", current.get("refine_model", "")),
            ("refine_device", "精炼设备 (cpu/cuda)", current.get("refine_device", "cuda")),
            ("refine_max_tokens", "精炼 Max Tokens", str(current.get("refine_max_tokens", 512))),
            ("refine_enable_thinking", "启用 Thinking 模式", current.get("refine_enable_thinking", False)),
            ("refine_api_base", "云端 API Base", current.get("refine_api_base", "")),
            ("refine_api_key", "云端 API Key", current.get("refine_api_key", "")),
            ("refine_api_model", "云端 API 模型", current.get("refine_api_model", "")),

            # 上屏设置
            ("commit_backend", "上屏后端 (auto/wtype/xdotool/pynput)", current.get("commit_backend", "auto")),

            # 其他设置
            ("warmup", "启动时预热", current.get("warmup", True)),
            ("debug_diagnostics", "调试诊断", current.get("debug_diagnostics", False)),
            ("notify_backend", "通知后端 (auto/notify-send/none)", current.get("notify_backend", "auto")),
        ]

        entries: dict[str, tk.StringVar] = {}
        frm = ttk.Frame(scrollable_frame, padding=12)
        frm.pack(fill="both", expand=True)

        for row, (key, label, value) in enumerate(fields):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=str(value))
            entries[key] = var
            ttk.Entry(frm, textvariable=var, width=54).grid(row=row, column=1, sticky="we", pady=4)

        frm.columnconfigure(1, weight=1)
        status_var = tk.StringVar(value=f"配置文件: {self.config_path}")
        ttk.Label(frm, textvariable=status_var).grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=10)

        def _save() -> None:
            payload = {
                "hotkey": entries["hotkey"].get().strip(),
                "stop_hotkey": entries["stop_hotkey"].get().strip(),
                "toggle_hotkey": entries["toggle_hotkey"].get().strip(),
                "exit_hotkey": entries["exit_hotkey"].get().strip(),
                "cooldown_ms": int(entries["cooldown_ms"].get().strip() or "300"),
                "trigger_mode": entries["trigger_mode"].get().strip() or "ptt",
                "notify_backend": entries["notify_backend"].get().strip() or "auto",
                "duration": float(entries["duration"].get().strip() or "4.0"),
                "sample_rate": int(entries["sample_rate"].get().strip() or "16000"),
                "channels": int(entries["channels"].get().strip() or "1"),
                "input_device": entries["input_device"].get().strip() or "default",
                "record_format": entries["record_format"].get().strip() or "ogg",
                "record_backend": entries["record_backend"].get().strip() or "auto",
                "commit_backend": entries["commit_backend"].get().strip() or "auto",
                "asr_provider": entries["asr_provider"].get().strip() or "qwen-asr",
                "qwen_model": entries["qwen_model"].get().strip(),
                "qwen_language": entries["qwen_language"].get().strip() or "Chinese",
                "qwen_max_new_tokens": int(entries["qwen_max_new_tokens"].get().strip() or "1024"),
                "asr_context_preset": entries["asr_context_preset"].get().strip(),
                "asr_context": entries["asr_context"].get().strip(),
                "device": entries["device"].get().strip() or "cuda",
                "enable_text_refine": _parse_bool(entries["enable_text_refine"].get(), default=False),
                "refine_provider": entries["refine_provider"].get().strip() or "local",
                "refine_preset": entries["refine_preset"].get().strip() or "default",
                "refine_model": entries["refine_model"].get().strip(),
                "refine_device": entries["refine_device"].get().strip() or "cuda",
                "refine_max_tokens": int(entries["refine_max_tokens"].get().strip() or "512"),
                "refine_enable_thinking": _parse_bool(entries["refine_enable_thinking"].get(), default=False),
                "refine_api_base": entries["refine_api_base"].get().strip(),
                "refine_api_key": entries["refine_api_key"].get().strip(),
                "refine_api_model": entries["refine_api_model"].get().strip(),
                "warmup": _parse_bool(entries["warmup"].get(), default=True),
                "debug_diagnostics": _parse_bool(entries["debug_diagnostics"].get(), default=False),
                "hub": current.get("hub", "ms"),
                "remote_code": current.get("remote_code", ""),
                "hotword": current.get("hotword", []),
            }
            save_runtime_config(self.config_path, payload)
            status_var.set(f"已保存并重启后端 ({self.config_path})")
            self.restart_backend()

        btns = ttk.Frame(frm)
        btns.grid(row=len(fields) + 1, column=0, columnspan=2, sticky="e", pady=10)
        ttk.Button(btns, text="保存并重启", command=_save).pack(side="left", padx=6)
        ttk.Button(btns, text="关闭", command=win.destroy).pack(side="left", padx=6)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def open_quick_menu(self) -> None:
        # Create a temporary visible window for menu popup
        popup_win = tk.Toplevel(self.root)
        popup_win.withdraw()
        popup_win.overrideredirect(True)
        popup_win.geometry("1x1+0+0")
        popup_win.deiconify()
        popup_win.lift()
        popup_win.focus_force()

        menu = tk.Menu(popup_win, tearoff=0)
        menu.add_command(label=f"Status: {self.state.status}", state="disabled")

        # Last text display
        last_text = self.state.last_text[:50] + "..." if len(self.state.last_text) > 50 else self.state.last_text
        last_text_label = f"Last: {last_text}" if last_text else "Last: (无)"
        menu.add_command(label=last_text_label, state="disabled")

        # Performance stats display
        if self.state.last_total_ms > 0:
            perf_label = f"Perf: {self.state.last_total_ms:.0f}ms (录:{self.state.last_record_ms:.0f} 识:{self.state.last_transcribe_ms:.0f} 优:{self.state.last_refine_ms:.0f})"
        else:
            perf_label = "Perf: (无数据)"
        menu.add_command(label=perf_label, state="disabled")

        menu.add_separator()
        menu.add_command(label="Start Backend", command=self.start_backend)
        menu.add_command(label="Stop Backend", command=self.stop_backend)
        menu.add_separator()

        # Quick mode toggle
        config = load_runtime_config(self.config_path)
        quick_mode_enabled = not config.get("enable_text_refine", True)
        quick_mode_label = "✓ 快速模式（跳过文字优化）" if quick_mode_enabled else "  快速模式（跳过文字优化）"
        menu.add_command(label=quick_mode_label, command=lambda: self.toggle_quick_mode(not quick_mode_enabled))

        # Copy last text
        copy_state = "normal" if self.state.last_text else "disabled"
        menu.add_command(label="复制最后识别的文本", command=self.copy_last_text, state=copy_state)

        # Preset submenu
        preset_menu = tk.Menu(menu, tearoff=0)
        presets = ["default", "formal", "meeting", "summary", "technical"]
        current_preset = config.get("refine_preset", "default")

        for preset in presets:
            preset_label = f"✓ {preset}" if preset == current_preset else f"  {preset}"
            preset_menu.add_command(label=preset_label, command=lambda p=preset: self.switch_preset(p))

        menu.add_cascade(label="切换 Preset", menu=preset_menu)

        menu.add_command(label="Settings...", command=self.open_settings)
        menu.add_separator()
        menu.add_command(label="Quit", command=self.quit)

        # Get cursor position
        try:
            x = popup_win.winfo_pointerx()
            y = popup_win.winfo_pointery()
        except Exception as e:
            print(f"Failed to get pointer position: {e}", file=sys.stderr, flush=True)
            x, y = 100, 100

        try:
            menu.tk_popup(x, y)
        except Exception as e:
            pass  # popup failed, ignore
        finally:
            menu.grab_release()
            popup_win.after(100, popup_win.destroy)

    def _tray_callback(self, fn) -> Any:  # noqa: ANN401
        def _handler(icon=None, item=None):  # noqa: ANN001, ANN202
            del icon, item
            self.root.after(0, fn)

        return _handler

    def _start_tray(self) -> None:
        # Try AppIndicator3 first (native GNOME support)
        try:
            import gi
            gi.require_version('AppIndicator3', '0.1')
            gi.require_version('Gtk', '3.0')
            from gi.repository import AppIndicator3, Gtk
            self._start_appindicator(AppIndicator3, Gtk)
            return
        except (ImportError, ValueError) as e:
            print(f"AppIndicator3 not available: {e}", file=sys.stderr)
            print("Falling back to pystray", file=sys.stderr)

        # Fallback to pystray
        try:
            import pystray
            from PIL import Image
        except ModuleNotFoundError as exc:
            raise RuntimeError("GUI deps missing. Run: pip install -e '.[gui]'") from exc

        def _status_text(_: Any) -> str:
            return f"Status: {self.state.status}"

        # Try to use native menu first, fallback to tkinter popup menu
        has_menu = bool(getattr(pystray.Icon, "HAS_MENU", True))

        # Load initial logo
        logo_path = get_logo_path("idle")
        image = load_svg_as_image(logo_path, size=(64, 64))

        if has_menu:
            # Use native pystray menu
            def _quick_mode_checked(_: Any) -> bool:
                config = load_runtime_config(self.config_path)
                return not config.get("enable_text_refine", True)

            def _toggle_quick_mode_pystray(icon, item):
                self.root.after(0, lambda: self.toggle_quick_mode(not item.checked))

            def _last_text_label(_: Any) -> str:
                last_text = self.state.last_text[:50] + "..." if len(self.state.last_text) > 50 else self.state.last_text
                return f"Last: {last_text}" if last_text else "Last: (无)"

            def _perf_label(_: Any) -> str:
                if self.state.last_total_ms > 0:
                    return f"Perf: {self.state.last_total_ms:.0f}ms (录:{self.state.last_record_ms:.0f} 识:{self.state.last_transcribe_ms:.0f} 优:{self.state.last_refine_ms:.0f})"
                return "Perf: (无数据)"

            def _preset_checked(preset_name: str):
                def _check(_: Any) -> bool:
                    config = load_runtime_config(self.config_path)
                    return config.get("refine_preset", "default") == preset_name
                return _check

            def _switch_preset_pystray(preset_name: str):
                def _handler(icon, item):
                    self.root.after(0, lambda: self.switch_preset(preset_name))
                return _handler

            presets = ["default", "formal", "meeting", "summary", "technical"]
            preset_items = [
                pystray.MenuItem(preset, _switch_preset_pystray(preset), checked=_preset_checked(preset), radio=True)
                for preset in presets
            ]

            menu = pystray.Menu(
                pystray.MenuItem(_status_text, self._tray_callback(lambda: None), enabled=False),
                pystray.MenuItem(_last_text_label, self._tray_callback(lambda: None), enabled=False),
                pystray.MenuItem(_perf_label, self._tray_callback(lambda: None), enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Start Backend", self._tray_callback(self.start_backend)),
                pystray.MenuItem("Stop Backend", self._tray_callback(self.stop_backend)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("快速模式（跳过文字优化）", _toggle_quick_mode_pystray, checked=_quick_mode_checked),
                pystray.MenuItem("复制最后识别的文本", self._tray_callback(self.copy_last_text), enabled=lambda _: bool(self.state.last_text)),
                pystray.MenuItem("切换 Preset", pystray.Menu(*preset_items)),
                pystray.MenuItem("Settings...", self._tray_callback(self.open_settings), default=True),
                pystray.MenuItem("Quit", self._tray_callback(self.quit)),
            )
            icon = pystray.Icon("recordian", image, "Recordian", menu)
        else:
            # Use tkinter popup menu fallback
            print("Native tray menu not supported, using tkinter popup menu fallback", file=sys.stderr, flush=True)
            print("Click the tray icon to open menu", file=sys.stderr, flush=True)

            def _on_activate(icon, item):
                print(f"Tray icon activated: icon={icon}, item={item}", file=sys.stderr, flush=True)
                self.root.after(0, self.open_quick_menu)

            # Create a simple menu with one item that opens the full menu
            menu = pystray.Menu(
                pystray.MenuItem("Open Menu", self._tray_callback(self.open_quick_menu), default=True),
            )
            icon = pystray.Icon("recordian", image, "Recordian", menu, on_activate=_on_activate)

        self.icon = icon
        self._tray_thread = threading.Thread(target=icon.run, daemon=True)
        self._tray_thread.start()

    def _start_appindicator(self, AppIndicator3, Gtk) -> None:
        """Start tray using AppIndicator3 (GNOME native)."""
        print("Using AppIndicator3 for tray icon", file=sys.stderr)

        from gi.repository import GLib
        self._gtk = Gtk
        self._glib = GLib

        # Resolve icon: try PNG conversion first, fall back to system theme icon
        logo_path = get_logo_path("idle")
        icon_path = str(logo_path.absolute())
        self._appindicator_png_cache: dict[str, str] = {}
        try:
            import cairosvg, tempfile, os
            fd, png_path = tempfile.mkstemp(suffix=".png")
            os.close(fd)
            png = Path(png_path)
            cairosvg.svg2png(url=icon_path, write_to=str(png), output_width=22, output_height=22)
            icon_path = str(png)
            self._appindicator_png_cache["idle"] = icon_path
            print(f"AppIndicator3 icon (PNG): {icon_path}", file=sys.stderr)
        except Exception as e:
            print(f"SVG→PNG conversion failed ({e}), using system icon", file=sys.stderr)
            icon_path = "audio-input-microphone"

        # Create indicator
        self.indicator = AppIndicator3.Indicator.new(
            "recordian",
            icon_path,
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Recordian")

        # Create menu
        menu = Gtk.Menu()

        # Status item (disabled)
        status_item = Gtk.MenuItem(label=f"Status: {self.state.status}")
        status_item.set_sensitive(False)
        menu.append(status_item)
        self._appindicator_status_item = status_item

        # Last text item (disabled, shows last recognized text)
        last_text = self.state.last_text[:50] + "..." if len(self.state.last_text) > 50 else self.state.last_text
        last_text_label = f"Last: {last_text}" if last_text else "Last: (无)"
        last_text_item = Gtk.MenuItem(label=last_text_label)
        last_text_item.set_sensitive(False)
        menu.append(last_text_item)
        self._appindicator_last_text_item = last_text_item

        # Performance stats item (disabled, shows last operation timing)
        if self.state.last_total_ms > 0:
            perf_label = f"Perf: {self.state.last_total_ms:.0f}ms (录:{self.state.last_record_ms:.0f} 识:{self.state.last_transcribe_ms:.0f} 优:{self.state.last_refine_ms:.0f})"
        else:
            perf_label = "Perf: (无数据)"
        perf_item = Gtk.MenuItem(label=perf_label)
        perf_item.set_sensitive(False)
        menu.append(perf_item)
        self._appindicator_perf_item = perf_item

        menu.append(Gtk.SeparatorMenuItem())

        # Start Backend
        start_item = Gtk.MenuItem(label="Start Backend")
        start_item.connect("activate", lambda _: self.root.after(0, self.start_backend))
        menu.append(start_item)

        # Stop Backend
        stop_item = Gtk.MenuItem(label="Stop Backend")
        stop_item.connect("activate", lambda _: self.root.after(0, self.stop_backend))
        menu.append(stop_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Quick Mode toggle
        quick_mode_item = Gtk.CheckMenuItem(label="快速模式（跳过文字优化）")
        config = load_runtime_config(self.config_path)
        quick_mode_enabled = not config.get("enable_text_refine", True)
        quick_mode_item.set_active(quick_mode_enabled)
        quick_mode_item.connect("toggled", lambda item: self.root.after(0, lambda: self.toggle_quick_mode(item.get_active())))
        menu.append(quick_mode_item)
        self._appindicator_quick_mode_item = quick_mode_item

        # Copy last text
        copy_text_item = Gtk.MenuItem(label="复制最后识别的文本")
        copy_text_item.connect("activate", lambda _: self.root.after(0, self.copy_last_text))
        copy_text_item.set_sensitive(bool(self.state.last_text))
        menu.append(copy_text_item)
        self._appindicator_copy_text_item = copy_text_item

        # Preset submenu
        preset_menu_item = Gtk.MenuItem(label="切换 Preset")
        preset_submenu = Gtk.Menu()

        presets = ["default", "formal", "meeting", "summary", "technical"]
        current_preset = config.get("refine_preset", "default")

        for preset in presets:
            preset_item = Gtk.RadioMenuItem(label=preset)
            if preset == current_preset:
                preset_item.set_active(True)
            preset_item.connect("activate", lambda item, p=preset: self.root.after(0, lambda: self.switch_preset(p)))
            preset_submenu.append(preset_item)

        preset_menu_item.set_submenu(preset_submenu)
        menu.append(preset_menu_item)

        # Settings
        settings_item = Gtk.MenuItem(label="Settings...")
        settings_item.connect("activate", lambda _: self.root.after(0, self.open_settings))
        menu.append(settings_item)

        menu.append(Gtk.SeparatorMenuItem())

        # Quit
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _: self.root.after(0, self.quit))
        menu.append(quit_item)

        menu.show_all()
        self.indicator.set_menu(menu)
        self.icon = None  # Mark that we're using AppIndicator instead

        # Start Gtk main loop in a background thread — required for AppIndicator3 to work
        self._gtk_thread = threading.Thread(target=Gtk.main, daemon=True, name="gtk-main")
        self._gtk_thread.start()
        print("Gtk main loop started", file=sys.stderr)

    def _update_tray_icon(self, status: str) -> None:
        """Update tray icon based on status."""
        if self.icon is None:
            return

        logo_path = get_logo_path(status)
        try:
            image = load_svg_as_image(logo_path, size=(64, 64))
            self.icon.icon = image
        except Exception:
            pass

    def _update_tray_menu(self) -> None:
        # Update AppIndicator status if using AppIndicator3
        if hasattr(self, 'indicator') and self.indicator is not None:
            status = self.state.status
            cache = getattr(self, '_appindicator_png_cache', {})
            if status not in cache:
                logo_path = get_logo_path(status)
                icon_path = str(logo_path.absolute())
                try:
                    import cairosvg, tempfile, os
                    fd, png_path = tempfile.mkstemp(suffix=".png")
                    os.close(fd)
                    png = Path(png_path)
                    cairosvg.svg2png(url=icon_path, write_to=str(png), output_width=22, output_height=22)
                    cache[status] = str(png)
                except Exception:
                    cache[status] = icon_path
            icon_path = cache[status]

            # Gtk operations must run on the Gtk thread — use GLib.idle_add
            if hasattr(self, '_glib'):
                indicator = self.indicator
                status_item = getattr(self, '_appindicator_status_item', None)
                label = f"Status: {status}"

                def _gtk_update():
                    if status_item is not None:
                        status_item.set_label(label)
                    # Update last text item
                    last_text_item = getattr(self, '_appindicator_last_text_item', None)
                    if last_text_item is not None:
                        last_text = self.state.last_text[:50] + "..." if len(self.state.last_text) > 50 else self.state.last_text
                        last_text_label = f"Last: {last_text}" if last_text else "Last: (无)"
                        last_text_item.set_label(last_text_label)
                    # Update performance stats item
                    perf_item = getattr(self, '_appindicator_perf_item', None)
                    if perf_item is not None:
                        if self.state.last_total_ms > 0:
                            perf_label = f"Perf: {self.state.last_total_ms:.0f}ms (录:{self.state.last_record_ms:.0f} 识:{self.state.last_transcribe_ms:.0f} 优:{self.state.last_refine_ms:.0f})"
                        else:
                            perf_label = "Perf: (无数据)"
                        perf_item.set_label(perf_label)
                    # Update copy text item sensitivity
                    copy_text_item = getattr(self, '_appindicator_copy_text_item', None)
                    if copy_text_item is not None:
                        copy_text_item.set_sensitive(bool(self.state.last_text))
                    try:
                        indicator.set_icon(icon_path)
                    except Exception:
                        pass

                self._glib.idle_add(_gtk_update)
            return

        # Update pystray icon
        if self.icon is not None:
            try:
                detail = _truncate(self.state.detail or self.state.status, 36)
                self.icon.title = f"Recordian | {self.state.status} | {detail}"
                self.icon.update_menu()
                # Update icon based on status
                self._update_tray_icon(self.state.status)
            except Exception:
                pass

    def quit(self) -> None:
        self.stop_backend()
        self.overlay.shutdown()

        # Stop AppIndicator if using it
        if hasattr(self, 'indicator') and self.indicator is not None:
            try:
                import gi
                gi.require_version('AppIndicator3', '0.1')
                from gi.repository import AppIndicator3
                self.indicator.set_status(AppIndicator3.IndicatorStatus.PASSIVE)
            except Exception:
                pass
            # Stop Gtk main loop
            if hasattr(self, '_glib') and hasattr(self, '_gtk'):
                try:
                    self._glib.idle_add(self._gtk.main_quit)
                except Exception:
                    pass

        # Stop pystray icon if using it
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                pass

        self.root.quit()
        self.root.destroy()


def _truncate(text: str, max_len: int) -> str:
    text = text.strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _parse_bool(value: str, *, default: bool) -> bool:
    token = value.strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _hex_with_alpha(color: str, alpha: float) -> str:
    # tkinter does not support #RRGGBBAA, so we blend against the dark bg.
    return _blend_hex("#0b0f1a", color, alpha)


def _blend_hex(a: str, b: str, ratio: float) -> str:
    ratio = max(0.0, min(1.0, ratio))
    a = a.lstrip("#")
    b = b.lstrip("#")
    if len(a) != 6 or len(b) != 6:
        return "#ffffff"
    ar, ag, ab = int(a[0:2], 16), int(a[2:4], 16), int(a[4:6], 16)
    br, bg, bb = int(b[0:2], 16), int(b[2:4], 16), int(b[4:6], 16)
    rr = int(ar * (1.0 - ratio) + br * ratio)
    rg = int(ag * (1.0 - ratio) + bg * ratio)
    rb = int(ab * (1.0 - ratio) + bb * ratio)
    return f"#{rr:02x}{rg:02x}{rb:02x}"


def main() -> None:
    args = build_parser().parse_args()
    app = TrayApp(args)
    app.run()


if __name__ == "__main__":
    main()
