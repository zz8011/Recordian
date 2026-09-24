"""Official Jev transport and product-corrector behavior without a network."""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

from recordian.streaming_correction import StreamingHotwordCorrector

_ALIAS = [{"heard": "jeff", "word": "jev", "meaning": "软件工具"}]


def _write_script(tmp_path: Path, body: str) -> list[str]:
    path = tmp_path / "fake_jev.py"
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    return [sys.executable, str(path)]


def _corrector(tmp_path: Path, argv: list[str], **kwargs: object) -> StreamingHotwordCorrector:
    return StreamingHotwordCorrector(
        list(kwargs.pop("hotwords", [])),  # type: ignore[arg-type]
        endpoint="",
        timeout_s=float(kwargs.pop("timeout_s", 0.8)),
        enabled=bool(kwargs.pop("enabled", True)),
        provider="jev",
        contextual_aliases=kwargs.pop("contextual_aliases", _ALIAS),
        jev_argv=argv,
        **kwargs,
    )


_TOOL_SCRIPT = """
import json, os, sys
assert os.environ.get("SEMIF") == "0"
assert os.environ.get("TYPESAFE_MODEL") == "jev-latest"
blob = " ".join(sys.argv)
if "jeff" in blob.lower() or "打开" in blob:
    sys.exit(4)
raw = sys.stdin.buffer.read()
data = json.loads(raw)
state = data["state"]
choice = "tool" if "工具" in state else "person"
probs = {"tool": 0.0, "person": 0.0, "unclear": 0.0}
probs[choice] = 0.9
probs["unclear" if choice != "unclear" else "person"] = 0.1
answer = {"type": "choice", "choice": choice, "probabilities": probs, "confidence": 0.9}
sys.stdout.write(json.dumps({"answers": {"role": answer}}))
"""


def test_jev_replaces_tool_span_and_keeps_parent_env(tmp_path: Path) -> None:
    before = os.environ.get("SEMIF")
    model = os.environ.get("TYPESAFE_MODEL")
    corrector = _corrector(tmp_path, _write_script(tmp_path, _TOOL_SCRIPT))
    try:
        text = "打开jeff工具检查这个项目"
        assert corrector.submit(text) == text
        assert corrector.finish(text) == "打开jev工具检查这个项目"
        assert os.environ.get("SEMIF") == before
        assert os.environ.get("TYPESAFE_MODEL") == model
    finally:
        corrector.close()


def test_jev_person_negation_code_and_repeat_do_not_replace(tmp_path: Path) -> None:
    corrector = _corrector(tmp_path, _write_script(tmp_path, _TOOL_SCRIPT))
    try:
        assert corrector.finish("我给Jeff发了邮件") == "我给Jeff发了邮件"
        assert corrector.finish("不要打开jeff") == "不要打开jeff"
        assert corrector.finish("运行`jeff --version`") == "运行`jeff --version`"
        assert corrector.finish("打开jeff工具，发给Jeff看") == "打开jeff工具，发给Jeff看"
    finally:
        corrector.close()


def test_disabled_and_empty_aliases_do_not_launch(tmp_path: Path) -> None:
    marker = tmp_path / "launched"
    script = _write_script(
        tmp_path,
        f"""
        from pathlib import Path
        Path({str(marker)!r}).write_text("ran", encoding="utf-8")
        """,
    )
    disabled = _corrector(tmp_path, script, enabled=False)
    empty = StreamingHotwordCorrector(
        ["时期"],
        endpoint="",
        timeout_s=0.5,
        enabled=True,
        provider="jev",
        contextual_aliases=[],
        jev_argv=script,
    )
    try:
        assert disabled.finish("打开jeff工具检查这个项目") == "打开jeff工具检查这个项目"
        assert empty.finish("屋里湿气很重") == "屋里湿气很重"
        assert empty.finish("打开jeff工具") == "打开jeff工具"
        assert not marker.exists()
    finally:
        disabled.close()
        empty.close()


def test_malformed_timeout_and_missing_binary_keep_original(tmp_path: Path) -> None:
    bad = _corrector(tmp_path, _write_script(tmp_path, "import sys\nsys.stdout.write('nope')\n"))
    missing = _corrector(tmp_path, ["/no/such/jev-binary"])
    slow = _corrector(
        tmp_path,
        _write_script(tmp_path, "import time\ntime.sleep(5)\n"),
        timeout_s=0.2,
    )
    try:
        text = "打开jeff工具检查这个项目"
        assert bad.finish(text) == text
        assert missing.finish(text) == text
        started = time.monotonic()
        assert slow.finish(text) == text
        assert time.monotonic() - started < 0.8
        time.sleep(0.05)
        assert slow.poll(text) == text
    finally:
        bad.close()
        missing.close()
        slow.close()


def test_weak_distribution_keeps_original(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        """
        import json, sys
        sys.stdin.buffer.read()
        answer = {"type": "choice", "choice": "tool", "probabilities": {"tool": 0.6, "person": 0.3, "unclear": 0.1}}
        sys.stdout.write(json.dumps({"answers": {"role": answer}}))
        """,
    )
    corrector = _corrector(tmp_path, script)
    try:
        text = "用jeff运行浏览器测试"
        assert corrector.finish(text) == text
    finally:
        corrector.close()


def test_rapid_submits_then_finish_uses_latest(tmp_path: Path) -> None:
    script = _write_script(
        tmp_path,
        """
        import json, sys, time
        raw = sys.stdin.buffer.read()
        time.sleep(0.08)
        data = json.loads(raw)
        state = data["state"]
        choice = "tool" if "检查这个项目" in state else "unclear"
        probs = {"tool": 0.05, "person": 0.05, "unclear": 0.05}
        probs[choice] = 0.9
        answer = {"type": "choice", "choice": choice, "probabilities": probs, "confidence": 0.9}
        sys.stdout.write(json.dumps({"answers": {"role": answer}}))
        """,
    )
    corrector = _corrector(tmp_path, script, timeout_s=1.2)
    try:
        assert corrector.submit("打开") == "打开"
        assert corrector.submit("打开jeff") == "打开jeff"
        final = "打开jeff工具检查这个项目"
        assert corrector.submit(final) == final
        started = time.monotonic()
        assert corrector.finish(final) == "打开jev工具检查这个项目"
        assert time.monotonic() - started < 1.2
        assert corrector.poll("打开jeff") == "打开jeff"
    finally:
        corrector.close()


def test_jev_path_does_not_require_requests(tmp_path: Path, monkeypatch) -> None:
    def boom() -> None:
        raise AssertionError("requests should not be imported for Jev")

    monkeypatch.setattr("recordian.semif_judge.require_requests", boom)
    corrector = _corrector(tmp_path, _write_script(tmp_path, _TOOL_SCRIPT))
    try:
        assert corrector.finish("打开jeff工具检查这个项目") == "打开jev工具检查这个项目"
    finally:
        corrector.close()
