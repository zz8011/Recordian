"""Cached versioned Rust GIO transport. Native failures never select busctl."""
from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Sequence

from .exceptions import CommitError
from .native_core import load_library

_MAX_STRING_BYTES = 1_048_576
_SIGNATURES = {
    "Ping": "", "BeginSession": "s", "UpdatePreedit": "ss", "CommitSegment": "sus",
    "CommitSession": "ss", "CancelSession": "s", "CommitText": "s",
}
_UNSET = object()
_selection: NativeBus | CommitError | None | object = _UNSET
_cache_lock = threading.RLock()
_cache_pid = os.getpid()


def _encode(value: str) -> bytes:
    try:
        encoded = value.encode("utf-8")
    except (AttributeError, UnicodeError) as exc:
        raise CommitError("org.recordian.Native.Error.InvalidArgument: expected UTF-8 string") from exc
    if b"\0" in encoded or len(encoded) > _MAX_STRING_BYTES:
        raise CommitError("org.recordian.Native.Error.InvalidArgument: NUL or oversized string")
    return encoded


class NativeBus:
    """One serialized connection; lazy connect avoids touching the bus on import."""

    def __init__(self, library: ctypes.CDLL, *, address: str | None = None) -> None:
        self._library = library
        self._address = _encode(address) if address is not None else None
        self._handle = ctypes.c_void_p()
        self._lock = threading.RLock()
        self._pid = os.getpid()
        self._closed = False
        self._connect_error: CommitError | None = None
        pointer = ctypes.POINTER(ctypes.c_void_p)
        signatures = {
            "recordian_dbus_abi_version": ([], ctypes.c_uint32),
            "recordian_dbus_new_v1": ([ctypes.c_char_p, pointer, pointer, pointer], ctypes.c_int),
            "recordian_dbus_call_v1": ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
                                       ctypes.POINTER(ctypes.c_char_p), ctypes.c_size_t,
                                       ctypes.c_int32, pointer, pointer, pointer], ctypes.c_int),
            "recordian_dbus_free_v1": ([ctypes.c_void_p], None),
            "recordian_dbus_string_free_v1": ([ctypes.c_void_p], None),
        }
        try:
            for name, (args, result) in signatures.items():
                function = getattr(library, name)
                function.argtypes = args
                function.restype = result
            if library.recordian_dbus_abi_version() != 1:
                raise CommitError("unsupported native D-Bus ABI")
        except AttributeError as exc:
            raise CommitError("native library is missing D-Bus ABI 1; transport cannot fall back") from exc

    def _consume(self, pointer: ctypes.c_void_p) -> str:
        if not pointer.value:
            return ""
        try:
            return ctypes.string_at(pointer).decode("utf-8", errors="strict")
        finally:
            self._library.recordian_dbus_string_free_v1(pointer)

    def _finish(self, status: int, output: ctypes.c_void_p,
                name: ctypes.c_void_p, message: ctypes.c_void_p) -> str:
        # Consume every allocation even when a malformed ABI returns invalid
        # UTF-8 or output together with an error. No borrowed C strings escape.
        values = []
        decoding_error = None
        for pointer in (output, name, message):
            try:
                values.append(self._consume(pointer))
            except UnicodeError as exc:
                values.append("")
                decoding_error = exc
        if decoding_error:
            raise CommitError("org.recordian.Native.Error.InvalidReply: invalid ABI UTF-8") from decoding_error
        reply, error_name, error_message = values
        if status:
            error = CommitError(f"{error_name or 'org.recordian.Native.Error.Transport'}: {error_message}")
            error.dbus_error_name = error_name
            raise error
        return reply

    def _connect(self) -> None:
        if self._connect_error is not None:
            raise self._connect_error
        if self._handle.value:
            return
        name, message = ctypes.c_void_p(), ctypes.c_void_p()
        status = self._library.recordian_dbus_new_v1(
            self._address, ctypes.byref(self._handle), ctypes.byref(name), ctypes.byref(message),
        )
        try:
            self._finish(status, ctypes.c_void_p(), name, message)
            if not self._handle.value:
                raise CommitError("native D-Bus returned no connection")
        except CommitError as exc:
            self._connect_error = exc
            raise

    def call(self, method: str, signature: str, args: Sequence[str], *, timeout_ms: int = 2000) -> str:
        if method not in _SIGNATURES or signature != _SIGNATURES[method] or len(args) != len(signature):
            raise CommitError("org.recordian.Native.Error.InvalidArgument: method/signature/arity")
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or not 1 <= timeout_ms <= 60_000:
            raise CommitError("org.recordian.Native.Error.InvalidArgument: timeout")
        encoded = [_encode(arg) for arg in args]
        pointers = (ctypes.c_char_p * len(encoded))(*encoded)
        with self._lock:
            if self._closed or self._pid != os.getpid():
                raise CommitError("org.recordian.Native.Error.SessionClosed: closed or inherited connection")
            self._connect()
            output, name, message = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
            status = self._library.recordian_dbus_call_v1(
                self._handle, _encode(method), _encode(signature), pointers, len(encoded), timeout_ms,
                ctypes.byref(output), ctypes.byref(name), ctypes.byref(message),
            )
            return self._finish(status, output, name, message)

    def close(self) -> None:
        # Never call GLib on a connection inherited across fork. GIO worker
        # threads were not inherited, so touching it can deadlock the child.
        if self._pid != os.getpid():
            self._closed = True
            return
        with self._lock:
            if not self._closed and self._handle.value:
                self._library.recordian_dbus_free_v1(self._handle)
                self._handle = ctypes.c_void_p()
            self._closed = True

    def __del__(self) -> None:
        if hasattr(self, "_closed"):
            self.close()


def get_transport() -> NativeBus | None:
    """Freeze transport selection before the first native call in this process.

    Only a loader-selected Python backend can return None. A library with a
    missing D-Bus ABI, a failed connection or a dispatched error fails closed.
    """
    global _selection, _cache_lock, _cache_pid
    if _cache_pid != os.getpid():
        _cache_lock = threading.RLock()
        _selection = _UNSET
        _cache_pid = os.getpid()
    with _cache_lock:
        if _selection is _UNSET:
            library = load_library()
            if library is None:
                _selection = None
            else:
                try:
                    _selection = NativeBus(library)
                except CommitError as exc:
                    _selection = exc
        if isinstance(_selection, CommitError):
            raise _selection
        return _selection  # type: ignore[return-value]
