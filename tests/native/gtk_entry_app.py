#!/usr/bin/env python3
"""GTK3 test input box for the Recordian fcitx addon native tests.

Runs under an isolated Xvfb display with GTK_IM_MODULE=fcitx and an
isolated dbus session. Protocol on stdout (line based, flushed):

  READY                     window mapped
  WID <xid>                 X window id of the window (main mode)
  TEXT <json string>        entry buffer changed (deduplicated)
  FOCUS_OUT / FOCUS_IN      entry focus transitions

Modes:
  default   one window with a focused Gtk.Entry (the dictation target)
  --other   a plain window used only to steal X focus

Exits when stdin is closed (so the driver can stop it) or on SIGTERM.
"""
from __future__ import annotations

import json
import signal
import sys

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402


def _emit(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main() -> int:
    other_mode = "--other" in sys.argv[1:]
    Gtk.init()

    window = Gtk.Window(title="recordian-native-other" if other_mode else "recordian-native-test")
    window.set_default_size(420, 140 if other_mode else 160)
    window.connect("destroy", Gtk.main_quit)

    last_text: list[str] = []

    if other_mode:
        box = Gtk.Box()
        button = Gtk.Button(label="focus sink")
        box.pack_start(button, True, True, 0)
        window.add(box)
    else:
        entry = Gtk.Entry()
        entry.set_placeholder_text("dictation target")
        window.add(entry)

        def on_changed(_widget) -> None:
            text = entry.get_text()
            if last_text and last_text[-1] == text:
                return
            last_text.append(text)
            _emit("TEXT " + json.dumps(text, ensure_ascii=False))

        entry.connect("changed", on_changed)

        last_preedit: list[str] = []

        def on_preedit_changed(_widget, preedit) -> None:
            # GtkEntry::preedit-changed carries the CURRENT preedit string as
            # its argument; connected handlers run BEFORE the default handler
            # updates the entry layout, so neither gtk_entry_get_preedit_string
            # (absent on GtkEntry) nor layout sampling can be used here.
            preedit = str(preedit or "")
            if last_preedit and last_preedit[-1] == preedit:
                return
            last_preedit.append(preedit)
            _emit("PREEDIT " + json.dumps(preedit, ensure_ascii=False))

        entry.connect("preedit-changed", on_preedit_changed)

        def on_focus_change(_widget, event):  # noqa: ANN001
            _emit("FOCUS_IN" if event.in_ else "FOCUS_OUT")

        window.connect("focus-in-event", on_focus_change)
        window.connect("focus-out-event", on_focus_change)

    def on_map(_window, _event) -> None:
        xid = window.get_window().get_xid()
        _emit(f"WID {xid}")
        if not other_mode:
            GLib.idle_add(entry.grab_focus)
            _emit("READY")

    window.connect("map-event", on_map)
    window.show_all()

    # Exit when the driver closes stdin (kept open for the process lifetime).
    # "SET <text>" line = programmatic GtkEntry.set_text(). NOTE (measured,
    # Recordian-22t): GTK3 set_text does NOT emit InputContext1.Reset on the
    # fcitx D-Bus protocol (only SetCursorRect) — s7 stays a LIMITATION.
    channel = GLib.IOChannel.unix_new(0)

    def _stdin_watch(_chan, cond):
        if cond & GLib.IO_HUP:
            GLib.idle_add(Gtk.main_quit)
            return False
        try:
            line = _chan.readline()
        except Exception:
            return True
        if line and line.startswith("SET ") and not other_mode:
            GLib.idle_add(entry.set_text, line[4:].rstrip("\n"))
        return True

    GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IO_IN | GLib.IO_HUP, _stdin_watch)

    signal.signal(signal.SIGTERM, lambda *_: GLib.idle_add(Gtk.main_quit))
    Gtk.main()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
