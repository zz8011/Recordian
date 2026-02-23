from recordian.linux_commit import _send_paste_shortcut


class _FakeKeyboard:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def press(self, key) -> None:  # noqa: ANN001
        self.events.append(("press", str(key)))

    def release(self, key) -> None:  # noqa: ANN001
        self.events.append(("release", str(key)))


class _FakeKey:
    ctrl = "ctrl"
    shift = "shift"
    insert = "insert"


def test_send_paste_shortcut_shift_insert() -> None:
    kb = _FakeKeyboard()
    detail = _send_paste_shortcut(kb, _FakeKey, "shift+insert")
    assert detail == "pasted_from_clipboard_shift_insert"
    assert kb.events == [
        ("press", "shift"),
        ("press", "insert"),
        ("release", "insert"),
        ("release", "shift"),
    ]

