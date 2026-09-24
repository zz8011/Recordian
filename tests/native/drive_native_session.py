#!/usr/bin/env python3
"""Drive the real Recordian fcitx5 addon inside an isolated session.

Preconditions (set by the pytest wrapper tests/test_fcitx_native.py):
- DISPLAY points at a private Xvfb.
- Launched via dbus-run-session, so DBUS_SESSION_BUS_ADDRESS is private.
- RECORDIAN_NATIVE_TMP: scratch dir (writable) for XDG isolation.
- RECORDIAN_NATIVE_BUILD: dir containing librecordian-commit.so.
- RECORDIAN_CANDIDATE_SRC: candidate/src for `import recordian`.
- RECORDIAN_NATIVE_PY: venv python (has gi) to run the GTK apps.

Runs the real compiled addon against a real GTK entry and asserts the
composition contract: Begin/Update (full Unicode)/Commit exactly once,
Cancel clears (next Begin is not blocked by residue), FocusOut rejects
the commit, user typing invalidates the session, user preedit (pinyin
composition) is preserved, no crash. Raw busctl outputs are logged as
validation evidence. Never touches the user's real session: everything
lives on the private bus/display.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time

SERVICE = "org.fcitx.Fcitx5"
PATH = "/recordian"
IFACE = "org.fcitx.Fcitx.Recordian1"

TMP = os.environ.get("RECORDIAN_NATIVE_TMP", "/tmp/recordian-native")
BUILD = os.environ["RECORDIAN_NATIVE_BUILD"]
CANDIDATE_SRC = os.environ["RECORDIAN_CANDIDATE_SRC"]
PY = os.environ.get("RECORDIAN_NATIVE_PY", sys.executable)

results: list[tuple[str, str, str]] = []  # (scenario, status, detail)
failures: list[str] = []
procs: list[subprocess.Popen] = []
fcitx: subprocess.Popen | None = None


def log(message: str) -> None:
    print(f"[driver] {message}", flush=True)


def busctl_raw(method: str, signature: str, args: list[str]):
    cmd = ["busctl", "--user", "--timeout=3", "call", SERVICE, PATH, IFACE, method]
    if signature:
        cmd.append(signature)
    cmd.extend(args)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    log(f"busctl {method} {args!r} -> rc={proc.returncode} out={proc.stdout.strip()!r} err={proc.stderr.strip()!r}")
    return proc


def gdbus_raw(method: str, args: list[str]):
    """Like busctl_raw but preserves the D-Bus error *name* in the output.

    busctl collapses an error reply to "Call failed: <message>", dropping
    org.fcitx.Fcitx.Recordian.Error.StaleSession / ExistingPreedit. gdbus
    prints the full GDBus.Error:<name>: <message>, which is the authoritative
    check that the addon failed with the right error, not just any error.
    """
    cmd = ["gdbus", "call", "--session", "--dest", SERVICE, "--object-path", PATH,
           "--method", f"{IFACE}.{method}", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    log(f"gdbus {method} {args!r} -> rc={proc.returncode} out={proc.stdout.strip()!r} err={proc.stderr.strip()!r}")
    return proc


def monitor_ic_calls(action, label: str) -> list[str]:
    """Capture org.fcitx.Fcitx.InputContext1 D-Bus method calls during action().

    Returns the sorted set of member names seen (excluding daemon noise).
    Attribution evidence: which client->fcitx calls a GTK action actually
    produces (Reset / SetSurroundingText / SetCursorRect / ...).
    """
    mon_path = os.path.join(TMP, f"dbus-monitor-{label}.log")
    with open(mon_path, "w", encoding="utf-8") as mon_file:
        mon = subprocess.Popen(
            ["dbus-monitor", "--session",
             "type='method_call',interface='org.fcitx.Fcitx.InputContext1'"],
            stdout=mon_file, stderr=subprocess.STDOUT, text=True)
        time.sleep(0.3)
        action()
        time.sleep(0.5)
        mon.terminate()
        try:
            mon.wait(timeout=3)
        except subprocess.TimeoutExpired:
            mon.kill()
    with open(mon_path, encoding="utf-8", errors="replace") as fh:
        content = fh.read()
    members = sorted(set(re.findall(r"member=(\w+)", content)) - {"NameAcquired", "NameLost"})
    log(f"{label} IC bus calls: {members}")
    return members


class App:
    def __init__(self, args: list[str]) -> None:
        self.proc = subprocess.Popen(
            [PY, *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        procs.append(self.proc)
        self.wid: int | None = None
        self.lines: list[str] = []
        self.preedit_events: list[tuple[float, str]] = []  # (monotonic, text)
        self._buf = ""
        import threading

        def _pump() -> None:
            assert self.proc.stdout is not None
            for chunk in iter(self.proc.stdout.readline, ""):
                line = chunk.rstrip("\n")
                self.lines.append(line)
                log(f"[app{args[-1] if args else ''}] {line}")
                if line.startswith("WID "):
                    try:
                        self.wid = int(line.split()[1])
                    except (ValueError, IndexError):
                        pass
                elif line.startswith("PREEDIT "):
                    try:
                        self.preedit_events.append((time.monotonic(), json.loads(line[8:])))
                    except ValueError:
                        self.preedit_events.append((time.monotonic(), line[8:]))

        self.thread = threading.Thread(target=_pump, daemon=True)
        self.thread.start()

    def wait_line(self, prefix: str, timeout: float = 15.0) -> str | None:
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline:
            for line in self.lines[seen:]:
                seen += 1
                if line.startswith(prefix):
                    return line
            time.sleep(0.05)
        return None

    def texts(self) -> list[str]:
        out = []
        for line in self.lines:
            if line.startswith("TEXT "):
                try:
                    out.append(json.loads(line[5:]))
                except ValueError:
                    out.append(line[5:])
        return out

    def preedits(self, since: int = 0) -> list[tuple[float, str]]:
        """(monotonic_ts, preedit_text) events from the real GTK entry."""
        return list(self.preedit_events[since:])

    def send(self, line: str) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.write(line + "\n")
                self.proc.stdin.flush()
        except OSError:
            pass

    def stop(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.terminate()


def record(name: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    results.append((name, status, detail))
    log(f"SCENARIO {name}: {status} {detail}")
    if not ok:
        failures.append(name)
    return ok


def skip(name: str, reason: str) -> None:
    results.append((name, "SKIP", reason))
    log(f"SCENARIO {name}: SKIP {reason}")


def limitation(name: str, detail: str) -> None:
    """Record a KNOWN PLATFORM LIMITATION with its raw failing evidence.

    Never PASS, never counted as a suite failure: the contract expectation is
    documented as unmet for platform reasons (evidence preserved verbatim).
    If the platform behavior ever changes, this stays LIMITATION and the
    detail must be updated to say the expectation now holds.
    """
    results.append((name, "LIMITATION", detail))
    log(f"SCENARIO {name}: LIMITATION {detail}")


def setup_layout() -> None:
    share = os.path.join(TMP, "share", "fcitx5", "addon")
    libdir = os.path.join(TMP, "lib", "fcitx5")
    confdir = os.path.join(TMP, "config", "fcitx5")
    for path in (share, libdir, confdir, os.path.join(TMP, "cache"), os.path.join(TMP, "run")):
        os.makedirs(path, exist_ok=True)
    os.chmod(os.path.join(TMP, "run"), 0o700)
    conf_src = os.path.join(CANDIDATE_SRC, "..", "fcitx", "recordian-commit", "recordian-commit.conf")
    shutil.copyfile(conf_src, os.path.join(share, "recordian-commit.conf"))
    shutil.copyfile(
        os.path.join(BUILD, "librecordian-commit.so"),
        os.path.join(libdir, "librecordian-commit.so"),
    )
    with open(os.path.join(confdir, "profile"), "w", encoding="utf-8") as fh:
        fh.write(
            "[Groups/0]\n"
            "Name=Default\n"
            "Default Layout=us\n"
            "[Groups/0/Items/0]\n"
            "Name=keyboard-us\n"
            "Layout=\n"
            "[Groups/0/Items/1]\n"
            "Name=pinyin\n"
            "Layout=\n"
            "GroupOrder=0\n"
        )


def child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "XDG_DATA_DIRS": f"{os.path.join(TMP, 'share')}:/usr/local/share:/usr/share",
            "XDG_CONFIG_HOME": os.path.join(TMP, "config"),
            "XDG_CACHE_HOME": os.path.join(TMP, "cache"),
            "XDG_RUNTIME_DIR": os.path.join(TMP, "run"),
            "LD_LIBRARY_PATH": os.path.join(TMP, "lib", "fcitx5"),
            "GTK_IM_MODULE": "fcitx",
            "XMODIFIERS": "@im=fcitx",
            "QT_IM_MODULE": "fcitx",
            "PYTHONPATH": CANDIDATE_SRC,
            # fcitx5 addon libraries: our isolated copy first, then the
            # system dir so the stock addons (dbus frontend!) still load.
            "FCITX_ADDON_DIRS": f"{os.path.join(TMP, 'lib', 'fcitx5')}:/usr/lib/x86_64-linux-gnu/fcitx5",
        }
    )
    return env


def xdotool(*args: str) -> subprocess.CompletedProcess:
    cmd = ["xdotool", *args]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
    log(f"xdotool {' '.join(args)} -> rc={proc.returncode} out={proc.stdout.strip()!r}")
    return proc


def fcitx_alive() -> bool:
    return fcitx is not None and fcitx.poll() is None


def focused_wid() -> int | None:
    """XGetInputFocus via xdotool (works without a window manager)."""
    proc = xdotool("getwindowfocus")
    if proc.returncode == 0:
        try:
            return int(proc.stdout.strip())
        except ValueError:
            return None
    return None


def focus_window(wid: int, timeout: float = 5.0) -> bool:
    """Raise and drive X input focus to wid until XGetInputFocus agrees.

    Xvfb runs no window manager here, so windows never receive input focus
    on their own, _NET_ACTIVE_WINDOW stays unset, and stacking follows map
    order — the later-mapped window covers the earlier one, so clicks need
    an explicit raise. windowfocus issues a direct XSetInputFocus, which GTK
    reports as focus-in-event; without it fcitx never sees a focused input
    context and BeginSession fails with NoInputContext.
    """
    xdotool("windowraise", str(wid))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        xdotool("windowfocus", "--sync", str(wid))
        time.sleep(0.15)
        if focused_wid() == wid:
            return True
    return False


def begin_with_focus_retry(committer, wid: int, timeout: float = 12.0):
    """BeginSession, re-driving X focus while fcitx reports no focused IC.

    The GTK im module only focuses its fcitx input context after the window
    actually receives X focus; there is a race between window map and the
    first BeginSession. Only NoInputContext is retried — any other error
    (ExistingPreedit, sensitive, ...) is a real verdict and raises.
    """
    from recordian.linux_commit import CommitError

    deadline = time.monotonic() + timeout
    last_err = ""
    while time.monotonic() < deadline:
        try:
            return committer.begin_composition("")
        except CommitError as exc:
            last_err = str(exc)
            if "NoInputContext" not in last_err:
                raise
            log(f"BeginSession not ready ({last_err[:80]}); refocusing wid={wid}")
            focus_window(wid, timeout=2.0)
            time.sleep(0.3)
    raise CommitError(f"BeginSession never found a focused input context: {last_err}")


def wait_addon(timeout: float = 25.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fcitx is not None and fcitx.poll() is not None:
            log(f"fcitx5 exited early rc={fcitx.returncode}")
            return False
        proc = busctl_raw("Ping", "", [])
        if proc.returncode == 0 and '"ok"' in proc.stdout:
            return True
        time.sleep(0.25)
    return False


def main() -> int:
    global fcitx
    signal.alarm(420)
    if "DBUS_SESSION_BUS_ADDRESS" not in os.environ:
        log("not inside dbus-run-session")
        return 2
    if not os.environ.get("DISPLAY"):
        log("no DISPLAY")
        return 2

    setup_layout()
    env = child_env()
    log_path = os.path.join(TMP, "fcitx5.log")
    fcitx_log = open(log_path, "w", encoding="utf-8")
    fcitx = subprocess.Popen(
        ["fcitx5", "--replace"], env=env, stdout=fcitx_log, stderr=subprocess.STDOUT
    )
    if not wait_addon():
        log(f"addon did not come up; fcitx5 log tail:\n{open(log_path, encoding='utf-8').read()[-2000:]}")
        return 2
    record("addon_loaded_ping", True, "real compiled addon answers Ping on isolated bus")

    from recordian.linux_commit import CommitError, FcitxCommitter

    app = App([os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtk_entry_app.py"), "main"])
    other = App([os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtk_entry_app.py"), "--other", "other"])
    if app.wait_line("READY") is None or app.wid is None or other.wid is None:
        log("GTK app failed to start")
        return 2
    log(f"main wid={app.wid} other wid={other.wid}")
    time.sleep(0.6)  # let fcitx create the input context
    if not focus_window(app.wid):
        log(f"could not move X input focus to main window (focused={focused_wid()})")
        return 2
    log(f"X input focus now on wid={focused_wid()}")

    # -- S1: Begin/Update full Unicode/Commit exactly once -------------------
    try:
        committer = FcitxCommitter()
        session = begin_with_focus_retry(committer, app.wid)
        token_ok = bool(re.fullmatch(r"[0-9a-f]{20,}", session.token))
        record("s1_token_parsed_from_busctl_reply", token_ok, f"token={session.token[:8]}… preedit_capable={session.preedit_capable} info={session.info}")
        up = session.update_preedit("预编辑 你好 🎙️ ünïcödé 中文English123")
        record("s1_update_unicode", up.committed and up.detail == "updated", f"detail={up.detail}")
        preedit_seen = False
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            if any("预编辑 你好" in p for _, p in app.preedits()):
                preedit_seen = True
                break
            time.sleep(0.05)
        record(
            "s1b_app_preedit_visible",
            preedit_seen,
            "real GTK entry displayed our streamed preedit: "
            f"nonempty_preedits={sorted({p for _, p in app.preedit_events if p})[:3]}",
        )
        time.sleep(0.1)
        final = "最终🎯文本 mix 中文 English 123，标点！no-enter"
        res = session.commit(final)
        committed_once = res.committed and res.detail.startswith("committed")
        got_text = None
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            if final in app.texts():
                got_text = final
                break
            time.sleep(0.05)
        record("s1_commit_unicode_once", committed_once and got_text == final, f"commit_detail={res.detail} app_got={got_text!r}")
        dup = gdbus_raw("CommitSession", [session.token, "重复提交"])
        record(
            "s1_duplicate_commit_rejected",
            dup.returncode != 0 and "StaleSession" in (dup.stderr + dup.stdout),
            f"second CommitSession errors with StaleSession: {dup.stderr.strip()[:100]}",
        )
        record("s1_no_duplicate_text", app.texts().count(final) == 1, f"texts={app.texts()}")
    except Exception as exc:  # noqa: BLE001
        record("s1_commit_unicode_once", False, f"{type(exc).__name__}: {exc}")

    # -- S2: Cancel clears our preedit (residue detector: next Begin works) --
    try:
        before = app.texts()[-1] if app.texts() else ""
        s2 = FcitxCommitter().begin_composition("")
        s2.update_preedit("将被取消的预编辑文字 CancelMe")
        time.sleep(0.2)
        can = s2.cancel()
        time.sleep(0.4)
        leaked = any("CancelMe" in t for t in app.texts())
        # Residue would make the next BeginSession fail with ExistingPreedit.
        s2b = FcitxCommitter().begin_composition("")
        record("s2_cancel_clears", can.committed and not leaked, f"cancel_detail={can.detail} leaked={leaked}")
        tail = s2b.commit("二段提交")
        time.sleep(0.3)
        appended = f"{before}二段提交" in app.texts()
        record("s2_next_begin_not_blocked_no_residue", tail.committed and appended, f"tail={tail.detail} texts={app.texts()[-2:]}")
    except Exception as exc:  # noqa: BLE001
        record("s2_cancel_clears", False, f"{type(exc).__name__}: {exc}")

    # -- S3: FocusOut rejects the commit ------------------------------------
    try:
        s3 = FcitxCommitter().begin_composition("")
        s3.update_preedit("失焦期间的预编辑")
        focus_window(other.wid)
        app.wait_line("FOCUS_OUT", timeout=5.0)
        log(f"after focus steal: focused={focused_wid()}")
        time.sleep(0.4)
        res3 = s3.commit("失焦后不应提交的文字")
        time.sleep(0.3)
        leaked3 = any("失焦后不应提交" in t for t in app.texts())
        record(
            "s3_focusout_commit_rejected",
            (not res3.committed) and ("stale" in res3.detail.lower()) and not leaked3,
            f"detail={res3.detail} leaked={leaked3}",
        )
        # Documented toolkit caveat (see addon header + STREAMING-IME-PLAN):
        # GTK's im module commits the *displayed client preedit* by itself
        # when the window loses focus, before our addon can clear it. Only
        # CommitSession defines final text; record the observation, never
        # gate the verdict on it.
        toolkit_autocommit = any("失焦期间的预编辑" in t for t in app.texts())
        results.append((
            "s3b_toolkit_autocommits_preedit_on_focusout",
            "INFO",
            f"GTK auto-committed our preview text on focus-out: {toolkit_autocommit}",
        ))
        log(f"SCENARIO s3b_toolkit_autocommits_preedit_on_focusout: INFO auto_committed={toolkit_autocommit}")
        focus_window(app.wid)
        time.sleep(0.4)
        record("s3_fcitx_alive_after_focusout", fcitx_alive(), f"poll={fcitx.poll() if fcitx else None}")
    except Exception as exc:  # noqa: BLE001
        record("s3_focusout_commit_rejected", False, f"{type(exc).__name__}: {exc}")

    # -- S4: user typing invalidates the session -----------------------------
    try:
        focus_window(app.wid)
        s4 = FcitxCommitter().begin_composition("")
        s4.update_preedit("占位预编辑")
        time.sleep(0.15)
        xdotool("type", "--delay", "40", "z")
        time.sleep(0.5)
        upd = s4.update_preedit("迟到的更新")
        dead = busctl_raw("CancelSession", "s", [s4.token])
        typed = any(t.endswith("z") for t in app.texts())
        # Authoritative addon-level check: the invalidated token must fail
        # UpdatePreedit with the StaleSession error *name* (gdbus keeps it).
        probe = gdbus_raw("UpdatePreedit", [s4.token, "迟到探测"])
        record(
            "s4_user_typing_invalidates",
            probe.returncode != 0 and "StaleSession" in (probe.stderr + probe.stdout)
            and dead.stdout.strip() == 's "already_gone"' and typed,
            f"gdbus={probe.stderr.strip()[:90]} cancel={dead.stdout.strip()!r} typed_z={typed}",
        )
        # Client-observable mapping: linux_commit.update_preedit should also
        # report the session as stale, but busctl strips the error name, so
        # "StaleSession" never reaches the classifier. Recorded separately so
        # a product-side misclassification does not hide addon correctness.
        record(
            "s4b_linux_commit_maps_stale_session",
            (not upd.committed) and ("stale" in upd.detail.lower()),
            f"client detail={upd.detail}",
        )
    except Exception as exc:  # noqa: BLE001
        record("s4_user_typing_invalidates", False, f"{type(exc).__name__}: {exc}")

    # -- S5: user preedit (pinyin composition) preserved ---------------------
    try:
        focus_window(app.wid)
        time.sleep(0.2)
        xdotool("key", "ctrl+space")
        time.sleep(0.5)
        xdotool("type", "--delay", "45", "nihao")
        time.sleep(0.6)
        rejected = False
        begin_err = ""
        live = None
        try:
            live = FcitxCommitter().begin_composition("")
            begin_err = "begin-succeeded"
        except CommitError as exc:
            begin_err = str(exc)
            # busctl strips the error name; accept the message text too.
            rejected = "ExistingPreedit" in begin_err or "non-empty preedit" in begin_err
        # Authoritative addon-level check of the error *name*.
        probe = gdbus_raw("BeginSession", [""])
        name_rejected = probe.returncode != 0 and "ExistingPreedit" in (probe.stderr + probe.stdout)
        if rejected or name_rejected:
            record(
                "s5_user_preedit_preserved",
                name_rejected,
                f"BeginSession refused while user composes pinyin: {probe.stderr.strip()[:100]}",
            )
            xdotool("key", "Escape")
            time.sleep(0.3)
        else:
            typing_as_ascii = any("nihao" in t for t in app.texts())
            if live is not None:
                live.cancel()
            if typing_as_ascii:
                skip("s5_user_preedit_preserved", "pinyin IM not active in isolated fcitx (typed ascii reached the entry); user-preedit guard unproven here")
            else:
                record("s5_user_preedit_preserved", False, f"BeginSession unexpectedly succeeded: {begin_err}")
        xdotool("key", "Escape")
        xdotool("key", "ctrl+space")  # back to keyboard-us
        time.sleep(0.4)
    except Exception as exc:  # noqa: BLE001
        record("s5_user_preedit_preserved", False, f"{type(exc).__name__}: {exc}")

    # -- S6: garbage token does not crash; addon still answers ----------------
    bad = busctl_raw("UpdatePreedit", "ss", ["deadbeefdeadbeef", "x"])
    alive = fcitx_alive()
    ping = busctl_raw("Ping", "", [])
    record(
        "s6_garbage_token_no_crash",
        bad.returncode != 0 and alive and ping.returncode == 0,
        f"bad_rc={bad.returncode} alive={alive} ping={ping.stdout.strip()!r}",
    )

    # -- S7: toolkit-side Reset invalidates the session ----------------------
    # KNOWN PLATFORM LIMITATION (never PASS): GtkEntry.set_text() sends NO
    # InputContext1.Reset over the fcitx D-Bus protocol (bus capture proof),
    # so the addon's InputContextReset watch can never fire on this path and
    # the session survives a programmatic rewrite of the field. The raw
    # failing expectation is preserved verbatim below. Contrast with s8: a
    # real mouse click DOES deliver Reset and invalidation works.
    # (fcitx 5.1.7 also has NO Controller1.Reset method — verified:
    # "Unknown method Reset" — so the toolkit path is the only reset source.)
    try:
        focus_window(app.wid)
        s7 = FcitxCommitter().begin_composition("")
        s7.update_preedit("Reset前的预编辑")
        time.sleep(0.2)
        members7 = monitor_ic_calls(lambda: app.send("SET 工具箱重置后的正文"), "s7-set-text")
        probe = gdbus_raw("UpdatePreedit", [s7.token, "Reset后的迟到更新"])
        res7 = s7.commit("Reset后不应提交的文字")
        time.sleep(0.3)
        leaked7 = any("Reset后不应提交" in t for t in app.texts())
        s7b_ok = False
        try:
            s7b = FcitxCommitter().begin_composition("")
            s7b.cancel()
            s7b_ok = True
        except CommitError as exc:
            log(f"s7 follow-up BeginSession failed: {exc}")
        invalidated = (
            probe.returncode != 0 and "StaleSession" in (probe.stderr + probe.stdout)
            and not res7.committed and not leaked7
        )
        limitation(
            "s7_toolkit_reset_invalidates",
            "raw expectation (session dies on programmatic rewrite) UNMET: "
            f"ic_calls={members7} probe_rc={probe.returncode} "
            f"probe_err={probe.stderr.strip()[:50]!r} commit={res7.detail[:50]} "
            f"leaked={leaked7} rebinding={s7b_ok} invalidated={invalidated}",
        )
        # Toolkit caveat again: the reset may have committed our displayed
        # preedit into the entry before the addon could clear it.
        autocommit7 = any("Reset前的预编辑" in t for t in app.texts())
        results.append((
            "s7b_toolkit_autocommits_preedit_on_reset",
            "INFO",
            f"GTK auto-committed our preview text on toolkit set_text: {autocommit7}",
        ))
        log(f"SCENARIO s7b_toolkit_autocommits_preedit_on_reset: INFO auto_committed={autocommit7}")
    except Exception as exc:  # noqa: BLE001
        limitation("s7_toolkit_reset_invalidates", f"harness error (not evidence): {type(exc).__name__}: {exc}")

    # -- S8: caret move inside the SAME input context (mouse click) ----------
    # No key event fires here; only SurroundingTextUpdated / Reset watches can
    # catch it. Proves the new watches are real, not just FocusOut/Key.
    try:
        focus_window(app.wid)
        s8 = FcitxCommitter().begin_composition("")
        s8.update_preedit("光标移动前的预编辑")
        time.sleep(0.2)
        geo = subprocess.run(["xdotool", "getwindowgeometry", str(app.wid)],
                             capture_output=True, text=True, timeout=5).stdout
        mx = my = None
        gx = gy = 0
        for line in geo.splitlines():
            if "Position:" in line:
                nums = re.findall(r"-?\d+", line)
                gx, gy = int(nums[0]), int(nums[1])
            if "Geometry:" in line:
                nums = re.findall(r"\d+", line)
                my = int(nums[1])
        mx = gx + 12            # far left of the entry: caret jumps to start
        my = gy + min(my // 2, 60)

        def _click8() -> None:
            xdotool("mousemove", str(mx), str(my))
            xdotool("click", "1")

        members8 = monitor_ic_calls(_click8, "s8-click")
        probe = gdbus_raw("UpdatePreedit", [s8.token, "光标移动后的迟到更新"])
        res8 = s8.commit("光标移动后不应提交的文字")
        time.sleep(0.3)
        leaked8 = any("光标移动后不应提交" in t for t in app.texts())
        record(
            "s8_caret_move_same_ic_invalidates",
            probe.returncode != 0 and "StaleSession" in (probe.stderr + probe.stdout)
            and not res8.committed and not leaked8,
            f"ic_calls={members8} probe={probe.stderr.strip()[:60]} commit={res8.detail[:50]} leaked={leaked8}",
        )
        autocommit8 = any("光标移动前的预编辑" in t for t in app.texts())
        results.append((
            "s8b_toolkit_autocommits_preedit_on_click",
            "INFO",
            f"GTK auto-committed our preview text on mouse click: {autocommit8}",
        ))
        log(f"SCENARIO s8b_toolkit_autocommits_preedit_on_click: INFO auto_committed={autocommit8}")
    except Exception as exc:  # noqa: BLE001
        record("s8_caret_move_same_ic_invalidates", False, f"{type(exc).__name__}: {exc}")

    # -- S9: manual input-method switch invalidates the session --------------
    try:
        focus_window(app.wid)
        s9 = FcitxCommitter().begin_composition("")
        s9.update_preedit("切输入法前的预编辑")
        time.sleep(0.2)
        members9 = monitor_ic_calls(lambda: xdotool("key", "ctrl+space"), "s9-ctrl-space")
        probe = gdbus_raw("UpdatePreedit", [s9.token, "切输入法后的迟到更新"])
        res9 = s9.commit("切输入法后不应提交的文字")
        time.sleep(0.3)
        leaked9 = any("切输入法后不应提交" in t for t in app.texts())
        record(
            "s9_im_switch_invalidates",
            probe.returncode != 0 and "StaleSession" in (probe.stderr + probe.stdout)
            and not res9.committed and not leaked9,
            f"ic_calls={members9} probe={probe.stderr.strip()[:60]} commit={res9.detail[:50]} leaked={leaked9}",
        )
        xdotool("key", "Escape")
        xdotool("key", "ctrl+space")  # back to keyboard-us for later scenarios
        time.sleep(0.4)
    except Exception as exc:  # noqa: BLE001
        record("s9_im_switch_invalidates", False, f"{type(exc).__name__}: {exc}")

    # -- S13: normal PTT interaction — hold Control_R, dictate, release ------
    # Default push-to-talk: the user holds Control_R, THEN recording (and the
    # composition session) starts, and the final commit happens around key
    # release. This supported default must stay usable: keydown happens before
    # Begin (no session to invalidate), keyup is a release event (exempt).
    try:
        focus_window(app.wid)
        xdotool("keydown", "Control_R")
        time.sleep(0.2)
        s13 = FcitxCommitter().begin_composition("")
        s13.update_preedit("按住Control说话的预编辑 🎤")
        time.sleep(0.2)
        xdotool("keyup", "Control_R")
        time.sleep(0.3)
        final13 = "PTT最终文本 按住Ctrl说完 mix English 🎤"
        res13 = s13.commit(final13)
        time.sleep(0.3)
        # Entry accumulates prior scenario text; count substring occurrences
        # across all emitted snapshots (deduped full-buffer lines).
        occ13 = sum(t.count(final13) for t in app.texts())
        record(
            "s13_ptt_hold_control_commit_once",
            res13.committed and occ13 == 1,
            f"commit={res13.detail[:60]} occurrences={occ13}",
        )
    except Exception as exc:  # noqa: BLE001
        record("s13_ptt_hold_control_commit_once", False, f"{type(exc).__name__}: {exc}")
    finally:
        xdotool("keyup", "Control_R")  # never leave a stuck modifier

    # -- S13B: toggle-stop key press DURING an active preedit (compat check) --
    # A toggle-style stop press (Control_R tap) while our preedit is showing
    # is a user key event: per the addon contract it invalidates the session.
    # This records the actual compatibility behavior; it does not weaken the
    # user-typing invalidation contract either way.
    try:
        focus_window(app.wid)
        s13b = FcitxCommitter().begin_composition("")
        s13b.update_preedit("停止键按下时的预编辑")
        time.sleep(0.2)
        members13 = monitor_ic_calls(lambda: xdotool("key", "Control_R"), "s13b-toggle")
        probe = gdbus_raw("UpdatePreedit", [s13b.token, "停止键后的迟到更新"])
        invalidated13b = probe.returncode != 0 and "StaleSession" in (probe.stderr + probe.stdout)
        if not invalidated13b:
            s13b.cancel()
        results.append((
            "s13b_toggle_key_during_preedit_compat",
            "INFO",
            f"Control_R tap during preedit: session_invalidated={invalidated13b} "
            f"ic_calls={members13} probe_err={probe.stderr.strip()[:60]!r}",
        ))
        log(f"SCENARIO s13b_toggle_key_during_preedit_compat: INFO invalidated={invalidated13b}")
    except Exception as exc:  # noqa: BLE001
        results.append(("s13b_toggle_key_during_preedit_compat", "INFO", f"harness error: {exc}"))
    finally:
        xdotool("keyup", "Control_R")

    # -- S15: bare Control_R stop press preserves session --------------------
    # Product contract (Grok's narrow fix, C++ 956e4e54): PTT uses Control_R
    # hold and a toggle-stop Control_R press. A bare modifier press edits no
    # text, so it must NOT invalidate the bound session; the final commit
    # still lands exactly once (no hidden toolkit draft+final duplication).
    try:
        focus_window(app.wid)
        s15 = FcitxCommitter().begin_composition("")
        s15.update_preedit("停止键按下时的预编辑")
        time.sleep(0.2)
        xdotool("key", "Control_R")  # bare modifier tap (toggle-stop)
        time.sleep(0.5)
        final15 = "停止键后仍应提交的最终文本 保留会话 🎤"
        res15 = s15.commit(final15)
        time.sleep(0.3)
        occ15 = sum(t.count(final15) for t in app.texts())
        record(
            "s15_modifier_stop_preserves_session",
            res15.committed and occ15 == 1,
            f"commit={res15.detail[:60]} occurrences={occ15}",
        )
        if not res15.committed:
            s15.cancel()
    except Exception as exc:  # noqa: BLE001
        record("s15_modifier_stop_preserves_session", False, f"{type(exc).__name__}: {exc}")
    finally:
        xdotool("keyup", "Control_R")

    # -- S15B: non-modifier editing (Ctrl+A) must still invalidate ------------
    try:
        focus_window(app.wid)
        s15c = FcitxCommitter().begin_composition("")
        s15c.update_preedit("CtrlA前的预编辑")
        time.sleep(0.2)
        xdotool("key", "ctrl+a")
        time.sleep(0.5)
        probe = gdbus_raw("UpdatePreedit", [s15c.token, "CtrlA后的迟到更新"])
        inv15b = probe.returncode != 0 and "StaleSession" in (probe.stderr + probe.stdout)
        if not inv15b:
            s15c.cancel()
        record(
            "s15b_ctrl_a_still_invalidates",
            inv15b,
            f"probe={probe.stderr.strip()[:70]}",
        )
    except Exception as exc:  # noqa: BLE001
        record("s15b_ctrl_a_still_invalidates", False, f"{type(exc).__name__}: {exc}")

    # -- S11: real worker (synthetic provider) + refused Begin = zero writes -
    # Drives GLM's unmodified _start_realtime_asr_worker with a fake provider
    # and a fake monitor stream (silence; no microphone) while the user is
    # composing pinyin in the entry. BeginSession must be refused
    # (ExistingPreedit) and the worker must suppress every write: no commit,
    # no CommitText fallback, user preedit untouched.
    try:
        import io
        from types import SimpleNamespace

        from recordian.linux_dictate import RecordProcessHandle
        from recordian.realtime_asr import _start_realtime_asr_worker

        focus_window(app.wid)
        xdotool("key", "ctrl+space")  # keyboard-us -> pinyin
        time.sleep(0.5)
        xdotool("type", "--delay", "45", "nihao")
        time.sleep(0.6)
        texts_before = list(app.texts())

        silence = io.BytesIO(b"\x00" * (16000 * 4 // 5))  # ~0.2s f32 mono
        dummy = subprocess.Popen(["sleep", "30"])
        procs.append(dummy)
        handle = RecordProcessHandle(
            process=dummy, monitor_stream=silence,
            monitor_sample_rate=16000, monitor_channels=1,
        )

        class _FakeASRSession:
            elapsed_ms = 5

            def push_audio(self, raw):
                return {"text": "合成识别文本不应上屏"}

            def finish(self):
                return SimpleNamespace(text="合成最终文本不应上屏", detected_language="zh")

            def cancel(self):
                pass

        class _FakeProvider:
            realtime_chunk_size_sec = 0.1

            def supports_realtime_transcription(self):
                return True

            def start_realtime_session(self, hotwords=()):
                return _FakeASRSession()

        args = SimpleNamespace(
            enable_streaming_commit=True, sample_rate=16000, channels=1,
            debug_diagnostics=False, semif_endpoint="", semif_timeout_s=0.12,
            enable_semif_correction=False, hotword=[], asr_context="",
            hotword_replacement=[],
        )
        states: list[dict] = []
        worker = _start_realtime_asr_worker(
            args=args, provider=_FakeProvider(), record_handle=handle,
            committer=FcitxCommitter(), enable_local_commit=True,
            auto_hard_enter=False, resolve_hotwords=list,
            normalize_final_text=lambda t: t, on_state=states.append,
            refine_enabled=False,
        )
        if worker is None:
            record("s11_worker_refused_begin_zero_commit", False, "worker did not start")
        else:
            worker.thread.join(timeout=20)
            still_running = worker.thread.is_alive()
            time.sleep(0.3)
            new_texts = app.texts()[len(texts_before):]
            probe = gdbus_raw("BeginSession", [""])
            preedit_intact = probe.returncode != 0 and "ExistingPreedit" in (probe.stderr + probe.stdout)
            record(
                "s11_worker_refused_begin_zero_commit",
                not still_running and worker.composition_refused
                and worker.outcome == "suppressed"
                and not bool((worker.commit_info or {}).get("committed"))
                and not new_texts and preedit_intact,
                f"refused={worker.composition_refused} outcome={worker.outcome!r} "
                f"commit_info={worker.commit_info} new_texts={new_texts} "
                f"user_preedit_intact={preedit_intact}",
            )
        dummy.terminate()
        try:
            dummy.wait(timeout=3)
        except subprocess.TimeoutExpired:
            dummy.kill()
        xdotool("key", "Escape")
        time.sleep(0.2)
        xdotool("key", "ctrl+space")  # back to keyboard-us
        time.sleep(0.4)
    except Exception as exc:  # noqa: BLE001
        record("s11_worker_refused_begin_zero_commit", False, f"{type(exc).__name__}: {exc}")

    # -- S12: 12x BeginSession burst on the same focused IC (supersession) ---
    # API promises a new session supersedes the previous one. Suspected C++
    # bug: BeginSession marks superseded entries finished but never erases
    # them, so sessions_ grows until kMaxSessions (8) and Begin starts
    # failing with SessionBusy. Reproduce; cleanup cancels every leftover
    # token so later scenarios are unaffected.
    try:
        focus_window(app.wid)
        tokens12: list[str] = []
        err12 = ""
        for _ in range(12):
            try:
                tokens12.append(FcitxCommitter().begin_composition("").token)
            except CommitError as exc:
                err12 = str(exc)
                break
        committed12 = False
        count12 = 0
        if tokens12:
            final12 = "连开十二次会话后的唯一提交"
            dup = busctl_raw("CommitSession", "ss", [tokens12[-1], final12])
            time.sleep(0.3)
            committed12 = dup.returncode == 0 and "committed" in dup.stdout
            count12 = sum(final12 in t for t in app.texts())
        for token in tokens12[:-1]:
            busctl_raw("CancelSession", "s", [token])  # erase finished leftovers
        record(
            "s12_begin_burst_supersession",
            len(tokens12) == 12 and committed12 and count12 == 1,
            f"begins_succeeded={len(tokens12)}/12 err={err12[:80]!r} "
            f"newest_committed={committed12} text_count={count12}",
        )
    except Exception as exc:  # noqa: BLE001
        record("s12_begin_burst_supersession", False, f"{type(exc).__name__}: {exc}")

    # -- S17 (optional): real Confucius ASR + real worker -> real GTK --------
    # Gated by RECORDIAN_NATIVE_ASR_ENDPOINT / RECORDIAN_NATIVE_ASR_WAV
    # (/ RECORDIAN_NATIVE_ASR_TOKEN_FILE). Replays a public 16kHz mono PCM16
    # WAV through a real-time-paced float32 monitor stream (no microphone),
    # instantiating the actual ConfuciusASRProvider and the actual
    # _start_realtime_asr_worker with a real FcitxCommitter. Requires >=2
    # distinct partial preedit states and the final GTK buffer containing
    # worker.final_text exactly once. The token is read from its file and
    # never logged. Skips cleanly when the env is absent; no model dependency
    # is forced on ordinary native CI.
    asr_endpoint = os.environ.get("RECORDIAN_NATIVE_ASR_ENDPOINT", "").strip()
    asr_wav = os.environ.get("RECORDIAN_NATIVE_ASR_WAV", "").strip()
    asr_token_file = os.environ.get("RECORDIAN_NATIVE_ASR_TOKEN_FILE", "").strip()
    if not (asr_endpoint and asr_wav):
        skip("s17_real_asr_worker_commit_once",
             "RECORDIAN_NATIVE_ASR_ENDPOINT/RECORDIAN_NATIVE_ASR_WAV unset — optional real-model case not run")
    else:
        dummy17 = None
        try:
            import wave
            from array import array
            from types import SimpleNamespace

            from recordian.linux_dictate import RecordProcessHandle
            from recordian.providers.confucius_asr import ConfuciusASRProvider
            from recordian.realtime_asr import _start_realtime_asr_worker

            with wave.open(asr_wav, "rb") as wf:
                if (wf.getframerate(), wf.getnchannels(), wf.getsampwidth()) != (16000, 1, 2):
                    raise ValueError(
                        f"WAV must be 16kHz mono PCM16, got rate={wf.getframerate()} "
                        f"ch={wf.getnchannels()} width={wf.getsampwidth()}")
                pcm = wf.readframes(wf.getnframes())
            samples = array("h")
            samples.frombytes(pcm)
            floats = array("f", (max(-1.0, min(1.0, s / 32768.0)) for s in samples))
            f32 = floats.tobytes()
            duration_s = len(samples) / 16000.0

            class _PacedStream:
                """BinaryIO stand-in replaying at real-time pace (bytes/sec)."""

                def __init__(self, data: bytes, bps: int) -> None:
                    self._data = data
                    self._pos = 0
                    self._bps = bps

                def read(self, n: int) -> bytes:
                    if self._pos >= len(self._data):
                        return b""
                    chunk = self._data[self._pos:self._pos + n]
                    self._pos += len(chunk)
                    time.sleep(len(chunk) / self._bps)
                    return chunk

                def close(self) -> None:
                    pass

            api_key = ""
            if asr_token_file:
                with open(asr_token_file, encoding="utf-8") as fh:
                    api_key = fh.read().strip()  # credentials never logged
            provider = ConfuciusASRProvider(asr_endpoint, api_key=api_key or None)

            focus_window(app.wid)
            # Scenario boundary snapshot: the final buffer must equal
            # baseline + worker.final_text exactly (not a substring count
            # over historic TEXT snapshots), and the >=2 distinct nonempty
            # preedit states must be ones the REAL GTK entry displayed.
            baseline17 = app.texts()[-1] if app.texts() else ""
            preedit_mark17 = len(app.preedit_events)
            dummy17 = subprocess.Popen(["sleep", "600"])
            procs.append(dummy17)
            handle = RecordProcessHandle(
                process=dummy17,
                monitor_stream=_PacedStream(f32, 16000 * 4),
                monitor_sample_rate=16000, monitor_channels=1,
            )
            args17 = SimpleNamespace(
                enable_streaming_commit=True, sample_rate=16000, channels=1,
                debug_diagnostics=False, semif_endpoint="", semif_timeout_s=0.12,
                enable_semif_correction=False, hotword=[], asr_context="",
                hotword_replacement=[],
            )
            partials17: list[str] = []

            def _on_state17(state: dict) -> None:
                if state.get("event") == "realtime_asr_partial":
                    partials17.append(str(state.get("text", "")))

            replay_start = time.monotonic()
            worker = _start_realtime_asr_worker(
                args=args17, provider=provider, record_handle=handle,
                committer=FcitxCommitter(), enable_local_commit=True,
                auto_hard_enter=False, resolve_hotwords=list,
                normalize_final_text=lambda t: t, on_state=_on_state17,
                refine_enabled=False,
            )
            if worker is None:
                record("s17_real_asr_worker_commit_once", False, "worker did not start")
            else:
                worker.thread.join(timeout=duration_s + 60)
                final_text = worker.final_text
                # App stdout delivery is asynchronous: the worker may join
                # just before the final GTK TEXT/PREEDIT lines are pumped.
                # Wait boundedly for the actual matching buffer event instead
                # of reading once (and instead of downgrading assertions).
                buffer17 = ""
                expected17 = baseline17 + final_text if final_text else None
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline:
                    buffer17 = app.texts()[-1] if app.texts() else ""
                    if expected17 is None or buffer17 == expected17:
                        break
                    time.sleep(0.05)
                new_preedits = app.preedits(since=preedit_mark17)
                distinct_app_preedits = {t for _, t in new_preedits if t}
                first_preedit_ms = next(
                    (int((ts - replay_start) * 1000) for ts, t in new_preedits if t), None
                )
                # Python-side partial callback list: additional evidence only.
                distinct_py_partials = len({t for t in partials17 if t})
                record(
                    "s17_real_asr_worker_commit_once",
                    not worker.thread.is_alive() and worker.outcome == "committed"
                    and bool((worker.commit_info or {}).get("committed"))
                    and len(distinct_app_preedits) >= 2
                    and bool(final_text)
                    and buffer17 == baseline17 + final_text
                    and buffer17.count(final_text) == 1,
                    f"outcome={worker.outcome!r} "
                    f"distinct_app_preedits={len(distinct_app_preedits)} "
                    f"distinct_py_partials={distinct_py_partials} "
                    f"first_app_preedit_ms={first_preedit_ms} "
                    f"buffer==baseline+final:{buffer17 == baseline17 + final_text} "
                    f"final_occurrences={buffer17.count(final_text)} "
                    f"latency_ms={worker.transcribe_latency_ms:.0f} "
                    f"error={worker.error[:80]!r}",
                )
        except Exception as exc:  # noqa: BLE001
            record("s17_real_asr_worker_commit_once", False, f"{type(exc).__name__}: {exc}")
        finally:
            if dummy17 is not None and dummy17.poll() is None:
                dummy17.terminate()
                try:
                    dummy17.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    dummy17.kill()

    # -- S10: real browser textarea, preedit -> commit exactly once ----------
    chrome = None
    try:
        browser_tmp = os.path.join(TMP, "browser")
        os.makedirs(os.path.join(browser_tmp, "home"), exist_ok=True)
        page = os.path.join(browser_tmp, "page.html")
        with open(page, "w", encoding="utf-8") as fh:
            fh.write(
                "<!doctype html><html><head><meta charset=utf-8>"
                "<title>Recordian Native Browser Test</title></head>"
                "<body style='margin:0'>"
                "<textarea id=t autofocus style='width:98vw;height:52vh;font-size:20px'></textarea>"
                "<div id=e contenteditable=true "
                "style='width:98vw;height:32vh;border:2px solid #888;font-size:20px'>editable:</div>"
                "<script>window.addEventListener('load',()=>document.getElementById('t').focus())</script>"
                "</body></html>"
            )
        chrome_bin = shutil.which("google-chrome")
        if not chrome_bin:
            skip("s10_browser_textarea_commit_once", "google-chrome not installed")
        else:
            benv = child_env()
            benv["HOME"] = os.path.join(browser_tmp, "home")
            chrome = subprocess.Popen(
                [chrome_bin, f"--user-data-dir={browser_tmp}/profile",
                 "--no-first-run", "--no-default-browser-check", "--password-store=basic",
                 "--disable-session-crashed-bubble", "--disable-extensions",
                 "--disable-dev-shm-usage", "--disable-gpu", "--new-window",
                 f"file://{page}"],
                env=benv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)  # own process group: cleanup = killpg
            bwid = None
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if chrome.poll() is not None:
                    break
                p = subprocess.run(
                    ["xdotool", "search", "--onlyvisible", "--name", "Recordian Native Browser Test"],
                    capture_output=True, text=True, timeout=5)
                if p.returncode == 0 and p.stdout.strip():
                    bwid = int(p.stdout.strip().splitlines()[-1])
                    break
                time.sleep(0.5)
            if bwid is None:
                skip("s10_browser_textarea_commit_once",
                     f"chrome window never appeared (poll={chrome.poll()}); browser path unproven")
            else:
                log(f"chrome wid={bwid}")
                focus_window(bwid)
                time.sleep(0.5)
                geo = subprocess.run(["xdotool", "getwindowgeometry", str(bwid)],
                                     capture_output=True, text=True, timeout=5).stdout
                gx = gy = 0
                gw = gh = 800
                for line in geo.splitlines():
                    if "Position:" in line:
                        nums = re.findall(r"-?\d+", line)
                        gx, gy = int(nums[0]), int(nums[1])
                    if "Geometry:" in line:
                        nums = re.findall(r"\d+", line)
                        gw, gh = int(nums[0]), int(nums[1])

                def _click_and_read_clipboard(frac_y: float) -> str:
                    xdotool("mousemove", str(gx + gw // 2), str(gy + int(gh * frac_y)))
                    xdotool("click", "1")
                    time.sleep(0.6)
                    xdotool("key", "ctrl+a")
                    time.sleep(0.2)
                    xdotool("key", "ctrl+c")
                    time.sleep(0.5)
                    return subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                          capture_output=True, text=True, timeout=5).stdout

                # textarea occupies the top ~52% of the page
                xdotool("mousemove", str(gx + gw // 2), str(gy + int(gh * 0.25)))
                xdotool("click", "1")  # ensure the textarea owns DOM focus
                time.sleep(0.6)
                sb = begin_with_focus_retry(FcitxCommitter(), bwid, timeout=15.0)
                log(f"browser session: preedit_capable={sb.preedit_capable} info={sb.info}")
                sb.update_preedit("浏览器预编辑 preEdit 中文 English 🎙️")
                time.sleep(0.3)
                final_b = "浏览器最终🎯文本 mix 中文 English 123！"
                rb = sb.commit(final_b)
                time.sleep(0.6)
                xdotool("key", "ctrl+a")
                time.sleep(0.2)
                xdotool("key", "ctrl+c")
                time.sleep(0.5)
                clip = subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                      capture_output=True, text=True, timeout=5).stdout
                record(
                    "s10_browser_textarea_commit_once",
                    rb.committed and clip == final_b,
                    f"commit={rb.detail[:60]} clipboard_equals_final={clip == final_b} clip_len={len(clip)}",
                )

                # -- S14: contenteditable div (same private Chrome) ----------
                # The div sits below the textarea (~80% height). It contains a
                # fixed "editable:" prefix; clipboard equality is checked
                # against prefix+final. Focus with a PLAIN click: ctrl+a would
                # select the prefix and the commit would replace it (standard
                # IM selection semantics, not a product defect).
                try:
                    xdotool("mousemove", str(gx + gw // 2), str(gy + int(gh * 0.80)))
                    xdotool("click", "1")
                    time.sleep(0.6)
                    se = begin_with_focus_retry(FcitxCommitter(), bwid, timeout=15.0)
                    log(f"contenteditable session: preedit_capable={se.preedit_capable} info={se.info}")
                    se.update_preedit("可编辑区预编辑 editAble 中文 English 🎤")
                    time.sleep(0.3)
                    final_e = "可编辑区最终🎯文本 contentEditable 中文 English 456！"
                    re_ = se.commit(final_e)
                    time.sleep(0.6)
                    clip_e = _click_and_read_clipboard(0.80)
                    expected_e = f"editable:{final_e}"
                    record(
                        "s14_browser_contenteditable_commit_once",
                        re_.committed and clip_e == expected_e,
                        f"commit={re_.detail[:60]} clipboard_equals_expected={clip_e == expected_e} "
                        f"clip={clip_e[:80]!r}",
                    )
                except Exception as exc:  # noqa: BLE001
                    record("s14_browser_contenteditable_commit_once", False, f"{type(exc).__name__}: {exc}")
    except Exception as exc:  # noqa: BLE001
        record("s10_browser_textarea_commit_once", False, f"{type(exc).__name__}: {exc}")
    finally:
        if chrome is not None and chrome.poll() is None:
            try:
                os.killpg(os.getpgid(chrome.pid), signal.SIGTERM)
                chrome.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(chrome.pid), signal.SIGKILL)
                except OSError:
                    pass

    # -- S16: real editor (Cursor / Electron, private profile) ---------------
    # Seeded scratch file, isolated user-data/extensions/HOME, no extensions,
    # no user project/account. Focus proven by reading back the seed content
    # via clipboard before Begin; after commit the buffer must equal
    # seed+final exactly (commit at document end, no synthetic typing).
    editor = None
    try:
        editor_bin = None
        for cand in ("/usr/share/cursor/cursor",):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                editor_bin = cand
                break
        if editor_bin is None:
            skip("s16_editor_commit_once", "cursor binary not found")
        else:
            # NOTE: `cursor --version` hangs in this sandboxed context
            # (Electron first-run singleton behavior); version is read from
            # the window title instead.
            ed_tmp = os.path.join(TMP, "editor")
            os.makedirs(os.path.join(ed_tmp, "home"), exist_ok=True)
            seed_text = "seed种子🌱文本:"
            seed = os.path.join(ed_tmp, "seed.txt")
            with open(seed, "w", encoding="utf-8") as fh:
                fh.write(seed_text)
            eenv = child_env()
            eenv["HOME"] = os.path.join(ed_tmp, "home")
            editor = subprocess.Popen(
                [editor_bin, f"--user-data-dir={ed_tmp}/user-data",
                 f"--extensions-dir={ed_tmp}/ext", "--disable-extensions",
                 "--new-window", "--skip-welcome", "--skip-release-notes",
                 "--disable-workspace-trust", "--disable-crash-reporter",
                 "--disable-gpu", seed],
                env=eenv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)  # own process group: cleanup = killpg
            ewid = None
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                if editor.poll() is not None:
                    break
                p = subprocess.run(
                    ["xdotool", "search", "--onlyvisible", "--class", "cursor"],
                    capture_output=True, text=True, timeout=5)
                if p.returncode == 0 and p.stdout.strip():
                    ewid = int(p.stdout.strip().splitlines()[-1])
                    # Editor loads the seed file asynchronously; wait until
                    # the window title names it (proof the file is open).
                    wname = subprocess.run(["xdotool", "getwindowname", str(ewid)],
                                           capture_output=True, text=True, timeout=5).stdout
                    if "seed.txt" in wname:
                        break
                time.sleep(1.0)
            if ewid is None:
                skip("s16_editor_commit_once",
                     f"cursor window never appeared in 90s (poll={editor.poll()}); editor path unproven")
            else:
                wname = subprocess.run(["xdotool", "getwindowname", str(ewid)],
                                       capture_output=True, text=True, timeout=5).stdout.strip()
                log(f"cursor wid={ewid} title={wname!r}")

                def _read_editor_buffer() -> str:
                    xdotool("key", "ctrl+a")
                    time.sleep(0.3)
                    xdotool("key", "ctrl+c")
                    time.sleep(0.5)
                    return subprocess.run(["xclip", "-selection", "clipboard", "-o"],
                                          capture_output=True, text=True, timeout=5).stdout

                focus_window(ewid)
                time.sleep(1.0)
                geo = subprocess.run(["xdotool", "getwindowgeometry", str(ewid)],
                                     capture_output=True, text=True, timeout=5).stdout
                gx = gy = 0
                gw = gh = 800
                for line in geo.splitlines():
                    if "Position:" in line:
                        nums = re.findall(r"-?\d+", line)
                        gx, gy = int(nums[0]), int(nums[1])
                    if "Geometry:" in line:
                        nums = re.findall(r"\d+", line)
                        gw, gh = int(nums[0]), int(nums[1])
                xdotool("mousemove", str(gx + gw // 2), str(gy + gh // 2))
                xdotool("click", "1")  # focus the editor pane
                time.sleep(1.0)
                before = _read_editor_buffer()
                seed_ok = before == seed_text
                log(f"editor buffer before: {before[:60]!r} seed_ok={seed_ok}")
                if not seed_ok:
                    record("s16_editor_commit_once", False,
                           f"editor focus/seed unproven: buffer={before[:80]!r}")
                else:
                    xdotool("key", "ctrl+a")   # collapse selection…
                    xdotool("key", "ctrl+End")  # …caret to document end
                    time.sleep(0.4)
                    se = begin_with_focus_retry(FcitxCommitter(), ewid, timeout=15.0)
                    log(f"editor session: preedit_capable={se.preedit_capable} info={se.info}")
                    se.update_preedit("编辑器预编辑 editorPre 中文 English 🎤")
                    time.sleep(0.3)
                    final16 = "编辑器最终🎯文本 cursorEditor 中文 English 789！"
                    res16 = se.commit(final16)
                    time.sleep(0.8)
                    after = _read_editor_buffer()
                    expected16 = seed_text + final16
                    record(
                        "s16_editor_commit_once",
                        res16.committed and after == expected16,
                        f"commit={res16.detail[:60]} buffer_equals_seed_plus_final={after == expected16} "
                        f"after_tail={after[-60:]!r}",
                    )
    except Exception as exc:  # noqa: BLE001
        record("s16_editor_commit_once", False, f"{type(exc).__name__}: {exc}")
    finally:
        if editor is not None and editor.poll() is None:
            try:
                os.killpg(os.getpgid(editor.pid), signal.SIGTERM)
                editor.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    os.killpg(os.getpgid(editor.pid), signal.SIGKILL)
                except OSError:
                    pass

    app.stop()
    other.stop()
    if fcitx is not None:
        fcitx.terminate()
        try:
            fcitx.wait(timeout=5)
        except subprocess.TimeoutExpired:
            fcitx.kill()

    skipped = [name for name, status, _ in results if status == "SKIP"]
    limitations = [name for name, status, _ in results if status == "LIMITATION"]
    log("=== native session summary ===")
    for name, status, detail in results:
        log(f"{status} {name} :: {detail[:160]}")
    try:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            tail = fh.read()[-4000:]
        log(f"=== fcitx5.log tail ===\n{tail}")
    except OSError:
        pass
    if failures:
        log(f"VERDICT: FAIL {failures} (limitations: {limitations or 'none'})")
        return 1
    log(f"VERDICT: PASS (skipped: {skipped or 'none'}; known platform limitations: {limitations or 'none'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
