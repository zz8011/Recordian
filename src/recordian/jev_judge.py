"""Bounded official-Jev transport through the installed ``jev ask`` CLI.

The CLI reads its own key. This module never sees credentials, never puts
the utterance on the command line, and never logs request or error bodies.
A missing binary, a malformed reply, a timeout, or a weak distribution
returns no accepted choices so the caller keeps the original text.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from .semif_judge import interpret_choice

_MAX_STDOUT = 1_000_000
_SYSTEMONE_PATH = "/v1/systemone"


def request_jev_choices(
    timeout_s: float,
    state: str,
    questions: dict[str, dict[str, Any]],
    *,
    endpoint: str = "",
    argv: Sequence[str] | None = None,
    on_process: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> dict[str, str | None]:
    """Ask one batch via ``jev ask``. Failure returns an empty dict.

    ``argv`` replaces the ``jev ask`` prefix (tests pass a local program).
    ``--timeout`` is appended. The utterance travels on stdin.
    jevkit 走本地 Plumb（POST ``<base>/v1/systemone``）。``endpoint`` 非空时
    以 ``SEMIF_BASE_URL`` 覆盖 plumb.json，留空则沿用 plumb.json。
    环境变量只改子进程。
    """
    if timeout_s <= 0 or not questions:
        return {}
    command = _command(argv, timeout_s)
    if not command:
        return {}
    payload = json.dumps(
        {"state": state, "questions": questions},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    env = os.environ.copy()
    # SEMIF=0 会关掉 jevkit 的本地 Plumb 路径，live 模式随即报 local_unconfigured。
    env.pop("SEMIF", None)
    base_url = plumb_base_url(endpoint)
    if base_url:
        env["SEMIF_BASE_URL"] = base_url
    # 让 Plumb 的 HTTP 超时不超过本次预算，避免沿用 plumb.json 的 8 秒。
    env["SEMIF_TIMEOUT"] = f"{max(0.05, timeout_s):.3f}"
    try:
        proc = subprocess.Popen(  # noqa: S603 - argv is a list, shell is off
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            shell=False,
        )
    except OSError:
        return {}
    if on_process is not None:
        on_process(proc)
    try:
        stdout, stderr = proc.communicate(payload, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return {}
    except OSError:
        _kill(proc)
        return {}
    if proc.returncode != 0 or not stdout or len(stdout) > _MAX_STDOUT:
        # Log stderr on failure so jev crashes, config errors, model load
        # failures are not silent. Limit to 500 chars to avoid log spam.
        if stderr and proc.returncode != 0:
            import logging
            logging.getLogger(__name__).warning(
                "jev ask failed (rc=%d): %s",
                proc.returncode,
                stderr.decode("utf-8", errors="replace")[:500],
            )
        return {}
    try:
        body = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(body, dict) or "error" in body:
        return {}
    answers = body.get("answers")
    if not isinstance(answers, dict):
        return {}
    results: dict[str, str | None] = {}
    for name, question in questions.items():
        criteria = question.get("criteria") if isinstance(question, dict) else None
        if not isinstance(criteria, dict):
            continue
        picked = answers.get(name)
        results[name] = interpret_choice(criteria, picked if isinstance(picked, dict) else None)
    return results


def plumb_base_url(endpoint: str) -> str:
    """``http://host:port/v1/systemone`` → ``http://host:port``；空串原样返回。"""
    base = str(endpoint or "").strip().rstrip("/")
    if base.endswith(_SYSTEMONE_PATH):
        base = base[: -len(_SYSTEMONE_PATH)].rstrip("/")
    return base


def _command(argv: Sequence[str] | None, timeout_s: float) -> list[str] | None:
    if argv:
        base = [str(part) for part in argv if str(part)]
        if not base:
            return None
    else:
        resolved = shutil.which("jev")
        if not resolved:
            return None
        base = [resolved, "ask"]
    return [*base, "--timeout", f"{max(0.05, timeout_s):.3f}"]


def _kill(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    try:
        proc.kill()
    except OSError:
        return
    try:
        proc.communicate(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        return
