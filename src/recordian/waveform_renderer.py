from __future__ import annotations

import queue
import threading
import time
import tkinter as tk


class WaveformRenderer:
    """波形动画渲染器：使用 pyglet/OpenGL shader 渲染音频可视化叠加层"""

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.state = "idle"
        self.amplitude = 0.0
        self.target_amplitude = 0.0
        self.level_boost = 0.0
        self.base_mode = 0.0
        self.detail = ""
        self.hide_deadline: float | None = None
        self.smooth_audio = 0.0
        self._cmd_queue: queue.SimpleQueue[tuple[str, object]] = queue.SimpleQueue()
        self._ready = threading.Event()
        self._init_error: Exception | None = None
        self._thread = threading.Thread(target=self._run_shader_loop, daemon=True)
        self._thread.start()
        # 异步初始化：不阻塞主线程，后台完成后通知
        # 如果需要检查初始化状态，使用 is_ready() 方法

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

    float vol = u_audio * u_motion;

    float len = length(uv * 1.8);
    vec2 distortedUV = uv;
    if(len < 1.0) {
        float as = tan(asin(len));
        distortedUV *= as * 0.4;
    }

    vec3 finalColor = vec3(0.0);

    vec3 c1 = vec3(0.1, 0.5, 1.0);
    vec3 c2 = vec3(0.8, 0.2, 0.9);
    vec3 c3 = vec3(0.1, 0.9, 0.7);
    vec3 c4 = vec3(1.0, 0.4, 0.4);

    for(int i = 0; i < 4; i++) {
        float fi = float(i);
        float baseSpeed = 0.15 + fi * 0.03;
        float volumeSpeed = vol * 0.95;
        float totalSpeed = baseSpeed + volumeSpeed;
        float t = u_time * totalSpeed;
        vec2 offset = vec2(
            sin(t + fi * 1.5) * 0.18,
            cos(t * 0.7 + fi * 2.0) * 0.12
        );
        float size = 0.28 + vol * 0.28 + sin(t * 0.5) * 0.05;
        float mask = SoftEllipse(distortedUV + offset, size, size * 0.7, 0.8);
        vec3 col = c1;
        if(i==1) col = c2;
        if(i==2) col = c3;
        if(i==3) col = c4;
        finalColor += col * mask * 0.7;
    }

    float core = SoftEllipse(distortedUV, 0.08 + vol * 0.08, 0.04, 0.95);
    finalColor += vec3(1.0, 1.0, 1.0) * core * 0.2;

    vec3 bg = vec3(0.02, 0.03, 0.08) * (1.0 - length(uv));

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
                width=352, height=352, caption="Recordian Overlay",
                style=overlay_style, resizable=False, visible=False, config=config,
            )
        except Exception:
            overlay_style = getattr(
                pyglet.window.Window,
                "WINDOW_STYLE_OVERLAY",
                pyglet.window.Window.WINDOW_STYLE_BORDERLESS,
            )
            window = pyglet.window.Window(
                width=352, height=352, caption="Recordian Overlay",
                style=overlay_style, resizable=False, visible=False,
            )
        window.set_vsync(False)
        gl.glClearColor(0.0, 0.0, 0.0, 0.0)
        gl.glEnable(gl.GL_BLEND)
        gl.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)
        try:
            window.set_mouse_passthrough(True)
        except Exception:
            pass

        display = window.display
        try:
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
                target_screen = None
                for screen in display.get_screens():
                    if (screen.x <= mouse_x < screen.x + screen.width and
                            screen.y <= mouse_y < screen.y + screen.height):
                        target_screen = screen
                        break
                if target_screen is None:
                    target_screen = display.get_default_screen()
            else:
                target_screen = display.get_default_screen()
        except Exception:
            target_screen = display.get_default_screen()

        _pos_x = max(0, target_screen.x + (target_screen.width - window.width) // 2)
        _pos_y = max(0, target_screen.y + target_screen.height - window.height - 80)

        try:
            from pyglet.libs.x11 import xlib as x11
            display = getattr(window, "_x_display", None)
            xwin = getattr(window, "_window", None)
            if display is not None and xwin is not None:
                wm_hints = x11.XAllocWMHints()
                if wm_hints:
                    wm_hints.contents.flags = x11.InputHint
                    wm_hints.contents.input = 0
                    x11.XSetWMHints(display, xwin, wm_hints)
                    x11.XFree(wm_hints)
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
            4, gl.GL_TRIANGLE_STRIP,
            position=("f", [-1.0, -1.0, 1.0, -1.0, -1.0, 1.0, 1.0, 1.0]),
        )
        start_time = time.monotonic()
        phase = 0.0

        @window.event
        def on_show() -> None:
            window.set_location(_pos_x, _pos_y)

        @window.event
        def on_draw() -> None:
            window.clear()
            program.use()
            program["u_resolution"] = (float(window.width), float(window.height))
            program["u_time"] = float(time.monotonic() - start_time + phase)
            program["u_audio"] = float(max(0.0, min(1.0, self.smooth_audio)))
            motion = 0.0
            if self.state == "recording":
                motion = max(0.0, min(1.0, (self.amplitude - 0.10) / 0.62))
            elif self.state == "processing":
                motion = 0.0
            program["u_motion"] = float(motion)
            quad.draw(gl.GL_TRIANGLE_STRIP)

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
                    # 降低触发阈值，普通语音也能驱动动画。
                    self.level_boost = max(0.0, level - 0.04)

            target = self.target_amplitude
            if self.state == "recording":
                target = min(1.0, target + self.level_boost * 0.70)
                self.level_boost *= 0.78
                self.smooth_audio += (self.amplitude - self.smooth_audio) * 0.34
            elif self.state == "processing":
                target = 0.0
                self.smooth_audio += (0.0 - self.smooth_audio) * 0.34
            else:
                self.smooth_audio += (0.0 - self.smooth_audio) * 0.34

            # 分离攻击/释放速率：减少快速抖动，保留语音跟随感。
            delta = target - self.amplitude
            attack = 0.065
            release = 0.033
            self.amplitude += delta * (attack if delta >= 0.0 else release)

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

    def is_ready(self) -> bool:
        """检查渲染器是否已初始化完成"""
        return self._ready.is_set()

    def get_init_error(self) -> Exception | None:
        """获取初始化错误（如果有）"""
        return self._init_error

    def shutdown(self) -> None:
        self._cmd_queue.put(("quit", None))
        self._thread.join(timeout=1.5)
