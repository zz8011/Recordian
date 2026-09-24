from recordian.text_cleanup import wrap_overlay_caption


def test_wrap_overlay_caption_keeps_short_text() -> None:
    assert wrap_overlay_caption("你好") == "你好"
    assert wrap_overlay_caption("  ") == ""


def test_wrap_overlay_caption_wraps_and_keeps_tail() -> None:
    caption = wrap_overlay_caption("一二三四五六七八九十" * 8, max_chars=10, max_lines=3)
    assert caption.startswith("…")
    assert caption.count("\n") == 2
    assert caption.endswith("九十")
