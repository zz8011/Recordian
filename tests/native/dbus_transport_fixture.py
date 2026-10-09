"""Fcitx-shaped service on an explicitly supplied private test bus."""
import json
import sys
import threading

from gi.repository import Gio, GLib

address, log_path = sys.argv[1:]
connection = Gio.DBusConnection.new_for_address_sync(
    address, Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION, None, None,
)
connection.set_exit_on_close(False)


def name_call(method):
    signature = "(su)" if method == "RequestName" else "(s)"
    values = ("org.fcitx.Fcitx5", 4) if method == "RequestName" else ("org.fcitx.Fcitx5",)
    return connection.call_sync(
        "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
        method, GLib.Variant(signature, values), None, 0, 2000, None,
    )


methods = {"Ping": "", "BeginSession": "s", "UpdatePreedit": "ss",
           "CommitSegment": "sus", "CommitSession": "ss", "CancelSession": "s",
           "CommitText": "s"}
xml = '<node><interface name="org.fcitx.Fcitx.Recordian1">' + ''.join(
    f'<method name="{method}">' + ''.join(
        f'<arg type="{kind}" direction="in"/>' for kind in signature
    ) + '<arg type="s" direction="out"/></method>'
    for method, signature in methods.items()
) + '</interface></node>'


def call(conn, sender, path, interface, method, args, invocation):
    values = args.unpack()
    with open(log_path, "a") as log:
        log.write(json.dumps({"method": method, "args": values, "sender": sender},
                             ensure_ascii=False) + "\n")
    text = values[-1] if values else ""
    if text == "__stale__" or (method == "CancelSession" and text == "stale-cancel-token"):
        invocation.return_dbus_error("org.fcitx.Fcitx.Recordian.Error.StaleSession", "focus lost")
        return
    if text == "__stale_lookalike__":
        invocation.return_dbus_error(
            "org.fcitx.Fcitx.Recordian.Error.StaleSessionExtra",
            "org.fcitx.Fcitx.Recordian.Error.StaleSession in message is not authoritative",
        )
        return
    if text == "__unsafe_lookalike__":
        invocation.return_dbus_error(
            "org.recordian.Fixture.Error.Other",
            "org.fcitx.Fcitx.Recordian.Error.SegmentsUnsafe is only message text",
        )
        return
    if text == "__error__":
        invocation.return_dbus_error("org.fcitx.Fcitx.Recordian.Error.SegmentsUnsafe", "rejected")
        return
    if text == "__wrong__":
        # Bypass invocation's introspection validation to send an actual wrong type.
        reply = Gio.DBusMessage.new_method_reply(invocation.get_message())
        reply.set_body(GLib.Variant("(u)", (7,)))
        conn.send_message(reply, Gio.DBusSendMessageFlags.NONE)
        return
    reply = {"Ping": "ok", "BeginSession": "server-token preedit=1 segments=1",
             "UpdatePreedit": "updated segments=1", "CancelSession": "cancelled",
             "CommitSession": "committed fixture", "CommitText": "committed fixture"}.get(method)
    if method == "BeginSession" and text == "__stale_cancel__":
        reply = "stale-cancel-token preedit=1 segments=1"
    if method == "CommitSegment":
        reply = f"segment {values[1]} fixture"

    def respond():
        invocation.return_value(GLib.Variant("(s)", (reply,)))
        return False

    if text == "__timeout__":
        GLib.timeout_add(250, respond)
    else:
        respond()


node = Gio.DBusNodeInfo.new_for_xml(xml)
connection.register_object("/recordian", node.interfaces[0], call, None, None)
assert name_call("RequestName").unpack() == (1,)
print("READY", flush=True)


def control():
    for command in sys.stdin:
        if command.strip() == "release":
            name_call("ReleaseName")
            print("RELEASED", flush=True)


threading.Thread(target=control, daemon=True).start()
GLib.MainLoop().run()
