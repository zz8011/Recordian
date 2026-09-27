from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from typing import cast

from recordian import orb_shader
from recordian.text_cleanup import wrap_overlay_caption


def desired_tick_interval_seconds(
    *,
    state: str,
    hide_deadline: float | None,
    active_tick_s: float,
    idle_tick_s: float,
) -> float:
    if state in {"recording", "processing", "error"}:
        return active_tick_s
    if hide_deadline is not None:
        return active_tick_s
    return idle_tick_s


def _set_mouse_passthrough_x11(window: object, passthrough: bool) -> bool:
    """X11 鼠标穿透切换。

    pyglet 的 ``XShapeCombineMask`` 绑定有类型错误（Display 按值声明，
    实际传入的是指针），导致 ``set_mouse_passthrough(False)`` 必然失败。
    这里统一改用类型正确的 ``XShapeCombineRegion``：穿透 = 空输入区域，
    可点击 = 覆盖整个窗口的输入区域。
    """
    try:
        import ctypes

        from pyglet.libs.x11 import xlib, xsync
    except Exception:
        return False
    display = getattr(window, "_x_display", None)
    xwin = getattr(window, "_window", None)
    if display is None or xwin is None:
        return False
    try:
        region = xlib.XCreateRegion()
        if not passthrough:
            rect = xlib.XRectangle(0, 0, int(getattr(window, "width", 0)), int(getattr(window, "height", 0)))
            xlib.XUnionRectWithRegion(ctypes.byref(rect), region, region)
        xsync.XShapeCombineRegion(display, xwin, xsync.ShapeInput, 0, 0, region, xsync.ShapeSet)
        xlib.XDestroyRegion(region)
        return True
    except Exception:
        return False


class WaveformRenderer:
    """波形动画渲染器：使用 pyglet/OpenGL shader 渲染音频可视化叠加层"""

    PROCESSING_HIDE_DELAY_S = 0.50
    ERROR_HIDE_DELAY_S = 1.55
    IDLE_HIDE_DELAY_WITH_DETAIL_S = 1.10
    IDLE_HIDE_DELAY_EMPTY_S = 0.35
    ACTIVE_TICK_S = 1.0 / 60.0
    IDLE_TICK_S = 1.0 / 12.0
    ORB_SIZE = 352
    CAPTION_HEIGHT = 96
    WINDOW_WIDTH = 560

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
        self._on_recording_click: object | None = None
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
        fragment_src = orb_shader.FRAGMENT_SRC
        try:
            config = gl.Config(double_buffer=True, alpha_size=8)  # type: ignore[abstract]
            overlay_style = getattr(
                pyglet.window.Window,
                "WINDOW_STYLE_OVERLAY",
                pyglet.window.Window.WINDOW_STYLE_BORDERLESS,
            )
            window = pyglet.window.Window(  # type: ignore[abstract]
                width=self.WINDOW_WIDTH,
                height=self.ORB_SIZE + self.CAPTION_HEIGHT,
                caption="Recordian Overlay",
                style=overlay_style, resizable=False, visible=False, config=config,
            )
        except Exception:
            overlay_style = getattr(
                pyglet.window.Window,
                "WINDOW_STYLE_OVERLAY",
                pyglet.window.Window.WINDOW_STYLE_BORDERLESS,
            )
            window = pyglet.window.Window(  # type: ignore[abstract]
                width=self.WINDOW_WIDTH,
                height=self.ORB_SIZE + self.CAPTION_HEIGHT,
                caption="Recordian Overlay",
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
            xdisp = getattr(window, "_x_display", None)
            xwin = getattr(window, "_window", None)
            if xdisp is not None and xwin is not None:
                wm_hints = x11.XAllocWMHints()
                if wm_hints:
                    wm_hints.contents.flags = x11.InputHint
                    wm_hints.contents.input = 0
                    x11.XSetWMHints(xdisp, xwin, wm_hints)
                    x11.XFree(wm_hints)
                sz_hints = x11.XAllocSizeHints()
                if sz_hints:
                    sz_hints.contents.flags = x11.PPosition | x11.USPosition
                    sz_hints.contents.x = _pos_x
                    sz_hints.contents.y = _pos_y
                    x11.XSetWMNormalHints(xdisp, xwin, sz_hints)
                    x11.XFree(sz_hints)
                x11.XFlush(xdisp)
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
        time_scale = orb_shader.apply_orb_uniforms(program, "idle")
        anim_time = 0.0
        orb_size = self.ORB_SIZE
        caption_h = self.CAPTION_HEIGHT
        orb_x = max(0, (window.width - orb_size) // 2)
        caption_label = None
        try:
            from pyglet.text import Label

            caption_label = Label(
                "",
                font_name=[
                    "Noto Sans CJK SC",
                    "Noto Sans CJK JP",
                    "WenQuanYi Micro Hei",
                    "Source Han Sans SC",
                    "Sans",
                ],
                font_size=15,
                x=window.width // 2,
                y=18,
                width=window.width - 32,
                anchor_x="center",
                anchor_y="bottom",
                align="center",
                multiline=True,
                color=(255, 255, 255, 235),
            )
        except Exception:
            caption_label = None

        @window.event
        def on_show() -> None:
            window.set_location(_pos_x, _pos_y)

        @window.event
        def on_mouse_press(x: int, y: int, button: int, modifiers: int) -> None:
            self._maybe_notify_recording_click()

        @window.event
        def on_draw() -> None:
            window.clear()
            gl.glViewport(orb_x, caption_h, orb_size, orb_size)
            program.use()
            program["u_size"] = (float(orb_size), float(orb_size))
            program["u_time"] = float(anim_time)
            audio = 0.0
            if self.state == "recording":
                audio = max(0.0, min(1.0, (self.amplitude - 0.05) / 0.50))
            program["u_audio"] = float(audio)
            quad.draw(gl.GL_TRIANGLE_STRIP)
            gl.glViewport(0, 0, window.width, window.height)
            if caption_label is not None:
                caption = wrap_overlay_caption(self.detail)
                caption_label.text = caption
                if caption:
                    caption_label.draw()

        current_tick_s = self.IDLE_TICK_S

        def _set_clickable(clickable: bool) -> None:
            """录音时允许点击（停止录音），其余状态鼠标穿透不挡操作。"""
            passthrough = not clickable
            if _set_mouse_passthrough_x11(window, passthrough):
                return
            try:
                window.set_mouse_passthrough(passthrough)
            except Exception:
                pass

        def _set_tick_interval(interval_s: float) -> None:
            nonlocal current_tick_s
            if abs(current_tick_s - interval_s) < 1e-6:
                return
            pyglet.clock.unschedule(update)
            pyglet.clock.schedule_interval(update, interval_s)
            current_tick_s = interval_s

        def _desired_tick_interval() -> float:
            return desired_tick_interval_seconds(
                state=self.state,
                hide_deadline=self.hide_deadline,
                active_tick_s=self.ACTIVE_TICK_S,
                idle_tick_s=self.IDLE_TICK_S,
            )

        def update(dt: float) -> None:
            nonlocal time_scale, anim_time
            anim_time += dt * time_scale
            while True:
                try:
                    cmd, payload = self._cmd_queue.get_nowait()
                except queue.Empty:
                    break
                if cmd == "quit":
                    pyglet.app.exit()
                    return
                if cmd == "state":
                    state, detail = cast(tuple[str, str], payload)
                    # 重复的同状态命令（如 realtime ASR partial 刷新 detail）
                    # 只做显示更新，不能重置振幅/相位，否则语音动画被打断。
                    state_changed = str(state) != self.state
                    self.state = str(state)
                    self.detail = str(detail)
                    self.hide_deadline = None
                    if state_changed:
                        time_scale = orb_shader.apply_orb_uniforms(program, self.state)
                    if self.state == "recording":
                        if state_changed:
                            self.target_amplitude = 0.0
                            self.level_boost = 0.0
                            self.amplitude = 0.0
                            self.base_mode = 1.0
                        window.set_location(_pos_x, _pos_y)
                        window.set_visible(True)
                        _set_clickable(True)
                    elif self.state == "processing":
                        self.target_amplitude = 0.0
                        self.amplitude = 0.0
                        self.base_mode = 0.0
                        self.level_boost = 0.0
                        _set_clickable(False)
                        self.hide_deadline = time.time() + self.PROCESSING_HIDE_DELAY_S
                    elif self.state == "error":
                        self.target_amplitude = 0.50
                        self.base_mode = 3.0
                        window.set_location(_pos_x, _pos_y)
                        window.set_visible(True)
                        _set_clickable(False)
                        self.hide_deadline = time.time() + self.ERROR_HIDE_DELAY_S
                    else:
                        self.target_amplitude = 0.0
                        self.base_mode = 0.0
                        _set_clickable(False)
                        delay = self.IDLE_HIDE_DELAY_WITH_DETAIL_S if self.detail.strip() else self.IDLE_HIDE_DELAY_EMPTY_S
                        self.hide_deadline = time.time() + delay
                elif cmd == "level":
                    level = max(0.0, min(1.0, float(payload)))  # type: ignore[arg-type]
                    # 上游已做平滑，这里直接跟随最新电平，不做峰值保持。
                    self.level_boost = max(0.0, level - 0.04)

            target = self.target_amplitude
            if self.state == "recording":
                target = min(1.0, target + self.level_boost)
                self.smooth_audio += (self.amplitude - self.smooth_audio) * 0.34
            elif self.state == "processing":
                target = 0.0
                self.smooth_audio += (0.0 - self.smooth_audio) * 0.34
            else:
                self.smooth_audio += (0.0 - self.smooth_audio) * 0.34

            # 分离攻击/释放速率：快速跟上音量起峰，回落稍缓保留平滑感。
            delta = target - self.amplitude
            attack = 0.35
            release = 0.15
            self.amplitude += delta * (attack if delta >= 0.0 else release)

            if self.hide_deadline is not None and time.time() >= self.hide_deadline:
                window.set_visible(False)
                _set_clickable(False)
                self.hide_deadline = None

            if window.visible:
                window.draw(dt)

            _set_tick_interval(_desired_tick_interval())

        pyglet.clock.schedule_interval(update, current_tick_s)
        self._ready.set()
        # Disable pyglet's default 60 FPS global redraw loop. The overlay draws
        # on its own adaptive tick so idle CPU actually drops with the slower rate.
        pyglet.app.run(None)

    def set_state(self, state: str, detail: str = "") -> None:
        self._cmd_queue.put(("state", (state, detail)))

    def set_level(self, level: float) -> None:
        self._cmd_queue.put(("level", float(level)))

    def set_on_recording_click(self, callback: object) -> None:
        """注册录音中点击 overlay 的回调（通常用于停止录音）。"""
        self._on_recording_click = callback

    def _maybe_notify_recording_click(self) -> None:
        if self.state != "recording":
            return
        callback = self._on_recording_click
        if callback is None:
            return
        try:
            callback()  # type: ignore[operator]
        except Exception:
            pass

    def is_ready(self) -> bool:
        """检查渲染器是否已初始化完成"""
        return self._ready.is_set()

    def get_init_error(self) -> Exception | None:
        """获取初始化错误（如果有）"""
        return self._init_error

    def shutdown(self) -> None:
        self._cmd_queue.put(("quit", None))
        self._thread.join(timeout=1.5)
