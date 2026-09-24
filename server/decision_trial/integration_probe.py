#!/usr/bin/env python3
"""StreamingCorrector integration probe for the Decider trial endpoint.

Product entry is ``recordian.streaming_correction.corrector_from_args``.
This tree has no ``create_streaming_corrector``. The factory builds a
``StreamingHotwordCorrector`` whose network path is ``request_choices``.

The probe keeps its endpoint, timeout, and alias in memory. It does not
read or write ``~/.config/recordian/hotkey.json``, and it does not open a
microphone, an input method, or another application.

It posts to the candidate only after ``reports/decider-warmup-final.json``
says ready. Otherwise it writes a prepared report and exits 2.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any

# <project>/server/decision_trial/integration_probe.py → <project>/src
DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
ENDPOINT = "http://192.168.5.111:42071/v1/systemone"
RUNTIME_TIMEOUT_S = 0.35
TIMEOUT_PROBE_S = 0.05
WAIT_READY_S = 30.0
# After finish returns, wait only for this probe's own HTTP hooks.
# Does not change the product deadline. A few hundred milliseconds.
OBSERVATION_WAIT_S = 0.4
ALIAS = {"heard": "jeff", "word": "jev", "meaning": "软件工具"}


def _ready_reason(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("ready") is True:
        return "ready"
    status = str(payload.get("status") or "").strip().lower()
    if status in {"ready", "passed", "pass"}:
        return f"status:{status}"
    readiness = payload.get("readiness")
    if isinstance(readiness, dict) and readiness.get("ready") is True:
        return "readiness.ready"
    if str(readiness or "").strip().lower() == "ready":
        return "readiness"
    return None


def wait_until_ready(path: Path, timeout_s: float) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout_s)
    last = "absent"
    while True:
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                last = f"unreadable:{type(exc).__name__}"
            else:
                reason = _ready_reason(payload)
                keys = sorted(payload)[:12] if isinstance(payload, dict) else []
                if reason is not None:
                    return {
                        "ready": True,
                        "reason": reason,
                        "waited_s": round(timeout_s - max(0.0, deadline - time.monotonic()), 3),
                        "keys": keys,
                    }
                last = f"not_ready keys={keys}"
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"ready": False, "reason": last, "waited_s": round(timeout_s, 3), "keys": []}
        time.sleep(min(2.0, remaining))


def _namespace(timeout_s: float, endpoint: str) -> argparse.Namespace:
    return argparse.Namespace(
        correction_provider="semif",
        enable_semif_correction=True,
        semif_endpoint=endpoint,
        semif_timeout_s=timeout_s,
        jev_timeout_s=1.5,
        contextual_aliases=[dict(ALIAS)],
    )


class _CallLog:
    """Records real Session.post / request_choices calls.

    ``begin``/``end`` belong to the probe hooks only. ``wait_idle`` blocks the
    probe thread after ``finish`` has already returned, so a late Timeout can
    be logged before the snapshot. It does not extend the product budget.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending = 0
        self._idle = threading.Event()
        self._idle.set()
        self.posts: list[dict[str, Any]] = []
        self.choices: list[dict[str, Any]] = []

    def begin(self) -> None:
        with self._lock:
            self._pending += 1
            self._idle.clear()

    def end(self) -> None:
        with self._lock:
            self._pending = max(0, self._pending - 1)
            if self._pending == 0:
                self._idle.set()

    def add_post(self, item: dict[str, Any]) -> None:
        with self._lock:
            self.posts.append(item)

    def add_choice(self, item: dict[str, Any]) -> None:
        with self._lock:
            self.choices.append(item)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"posts": list(self.posts), "choices": list(self.choices)}

    def wait_idle(self, timeout_s: float) -> bool:
        return self._idle.wait(timeout_s)


def _install_hooks(log: _CallLog) -> tuple[Any, Any]:
    import requests

    from recordian import semif_judge, streaming_correction

    real_post = requests.Session.post
    real_choices = streaming_correction.request_choices

    def post(self: Any, url: str, *args: Any, **kwargs: Any) -> Any:
        log.begin()
        started = time.perf_counter()
        timeout_s = kwargs.get("timeout")
        try:
            try:
                response = real_post(self, url, *args, **kwargs)
            except Exception as exc:
                log.add_post(
                    {
                        "url": url,
                        "timeout_s": timeout_s,
                        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                        "error": type(exc).__name__,
                    }
                )
                raise
            item: dict[str, Any] = {
                "url": url,
                "timeout_s": timeout_s,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "http_status": response.status_code,
            }
            try:
                body = response.json()
            except ValueError:
                item["body"] = "unreadable"
            else:
                if isinstance(body, dict):
                    item["model"] = body.get("model")
                    answers = body.get("answers")
                    if isinstance(answers, dict):
                        item["answer_keys"] = sorted(answers)
                        role = answers.get("role")
                        if isinstance(role, dict):
                            item["choice"] = role.get("choice")
            log.add_post(item)
            return response
        finally:
            log.end()

    def choices(session: Any, endpoint: str, timeout_s: float, state: str, questions: dict[str, Any]) -> dict[str, str | None]:
        log.begin()
        started = time.perf_counter()
        try:
            result = real_choices(session, endpoint, timeout_s, state, questions)
            log.add_choice(
                {
                    "endpoint": endpoint,
                    "timeout_s": timeout_s,
                    "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                    "state": state,
                    "question_names": sorted(questions),
                    "result": result,
                }
            )
            return result
        finally:
            log.end()

    requests.Session.post = post  # type: ignore[method-assign]
    streaming_correction.request_choices = choices
    semif_judge.request_choices = choices
    return real_post, real_choices


def _restore_hooks(real_post: Any, real_choices: Any) -> None:
    import requests

    from recordian import semif_judge, streaming_correction

    requests.Session.post = real_post  # type: ignore[method-assign]
    streaming_correction.request_choices = real_choices
    semif_judge.request_choices = real_choices


def _run_corrector(text: str, *, timeout_s: float, context: str, endpoint: str) -> dict[str, Any]:
    from recordian.streaming_correction import corrector_from_args
    from recordian.text_cleanup import _normalize_final_text

    normalized = _normalize_final_text(text)
    log = _CallLog()
    real_post, real_choices = _install_hooks(log)
    corrector = corrector_from_args(_namespace(timeout_s, endpoint), [], context=context)
    started = time.perf_counter()
    partial = polled = final = ""
    partial_ms = poll_ms = final_ms = 0.0
    observation_wait_ms = 0.0
    observation_idle = False
    http: dict[str, Any] = {"posts": [], "choices": []}
    try:
        partial = corrector.submit(normalized)
        partial_ms = round((time.perf_counter() - started) * 1000, 3)
        polled = corrector.poll(normalized)
        poll_ms = round((time.perf_counter() - started) * 1000, 3)
        final = corrector.finish(normalized)
        # Product finish time stops here. The wait below is probe observation only.
        final_ms = round((time.perf_counter() - started) * 1000, 3)
        wait_started = time.perf_counter()
        observation_idle = log.wait_idle(OBSERVATION_WAIT_S)
        observation_wait_ms = round((time.perf_counter() - wait_started) * 1000, 3)
        http = log.snapshot()
    finally:
        corrector.close()
        _restore_hooks(real_post, real_choices)
    return {
        "input": text,
        "normalized_input": normalized,
        "context": context,
        "timeout_s": timeout_s,
        "partial_output": partial,
        "poll_output": polled,
        "output": final,
        "partial_ms": partial_ms,
        "poll_ms": poll_ms,
        "elapsed_ms": final_ms,
        "observation_wait_ms": observation_wait_ms,
        "observation_idle": observation_idle,
        "http": http,
    }


def _local_case(text: str, expected: str) -> dict[str, Any]:
    from recordian.text_cleanup import _normalize_final_text

    started = time.perf_counter()
    output = _normalize_final_text(text)
    return {
        "id": "local_number_and_url",
        "kind": "local_normalize",
        "input": text,
        "expected": expected,
        "output": output,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
        "http": {"posts": [], "choices": []},
        "passed": output == expected,
        "note": "产品 recording_controller 使用的 text_cleanup._normalize_final_text。没有 HTTP，没有麦克风，没有输入法。",
    }


def _judge(case_id: str, expected: str, observed: dict[str, Any], *, require_http: bool, require_unchanged_on_timeout: bool) -> dict[str, Any]:
    posts = observed["http"]["posts"]
    errors = [item.get("error") for item in posts if item.get("error")]
    timeout_hit = any("Timeout" in str(name) for name in errors)
    output = observed["output"]
    passed = output == expected
    if require_http and not posts:
        passed = False
    if require_unchanged_on_timeout:
        passed = bool(posts) and timeout_hit and output == observed["normalized_input"]
    observed.update(
        {
            "id": case_id,
            "expected": expected,
            "passed": passed,
            "timeout_hit": timeout_hit,
            "post_errors": errors,
        }
    )
    return observed


def run_probe(endpoint: str) -> dict[str, Any]:
    from recordian.streaming_correction import corrector_from_args

    tool = _judge(
        "tool_jeff_to_jev",
        "今天下午先把jev安装好",
        _run_corrector("今天下午先把jeff安装好", timeout_s=RUNTIME_TIMEOUT_S, context="", endpoint=endpoint),
        require_http=True,
        require_unchanged_on_timeout=False,
    )
    tool["note"] = "软件语境。submit 立即返回规范化文本，poll 不额外等待，finish 最多等一个运行超时。"
    person = _judge(
        "person_jeff_unchanged",
        "Jeff帮我调试脚本",
        _run_corrector("Jeff帮我调试脚本", timeout_s=RUNTIME_TIMEOUT_S, context="", endpoint=endpoint),
        require_http=False,
        require_unchanged_on_timeout=False,
    )
    person["passed"] = person["output"] == person["expected"] and not person["http"]["posts"]
    person["note"] = "裸人名结构保护应直接保留 Jeff，且不发 role 请求。这不能证明所有人名都安全。"
    context_person = _judge(
        "person_jeff_with_software_context",
        "我打算找 Jeff 确认一下。",
        _run_corrector(
            "我打算找 Jeff 确认一下。",
            timeout_s=RUNTIME_TIMEOUT_S,
            context="文档里写了 jeff 工具的安装步骤",
            endpoint=endpoint,
        ),
        require_http=True,
        require_unchanged_on_timeout=False,
    )
    context_person["kind"] = "semantic"
    context_person["note"] = (
        "与榜单 ctx-person-04 相同：上一段含工具/安装，本句预期仍是人名。"
        "模型若判成 tool，产品会把 Jeff 改成 jev。这是真实语义错误，计入失败，不能藏在其它合同后面。"
    )
    local = _local_case("端口一百零二，访问https://example点com/path", "端口102，访问https://example.com/path")
    timeout_case = _judge(
        "timeout_keeps_original",
        "今天下午先把jeff安装好",
        _run_corrector("今天下午先把jeff安装好", timeout_s=TIMEOUT_PROBE_S, context="", endpoint=endpoint),
        require_http=True,
        require_unchanged_on_timeout=True,
    )
    timeout_case["note"] = (
        f"同一端点，内存超时 {TIMEOUT_PROBE_S}s。finish 耗时只计到产品返回；"
        f"随后最多再等 {OBSERVATION_WAIT_S}s 让探针自己的 post 钩子记下 Timeout。"
        "没有日志仍算失败。这不是麦克风录音超时。"
    )
    cases = [tool, person, context_person, local, timeout_case]
    failed_ids = [item["id"] for item in cases if not item["passed"]]
    return {
        "status": "executed",
        "factory": "recordian.streaming_correction.corrector_from_args",
        "factory_callable": corrector_from_args.__name__,
        "create_streaming_corrector_present": hasattr(
            sys.modules["recordian.streaming_correction"], "create_streaming_corrector"
        ),
        "endpoint": endpoint,
        "runtime_timeout_s": RUNTIME_TIMEOUT_S,
        "timeout_probe_s": TIMEOUT_PROBE_S,
        "observation_wait_s": OBSERVATION_WAIT_S,
        "alias": ALIAS,
        "user_config_read": False,
        "user_config_written": False,
        "microphone_used": False,
        "ime_used": False,
        "other_app_input": False,
        "real_recording": False,
        "covers_all_typos": False,
        "cases": cases,
        "cases_total": len(cases),
        "cases_passed": len(cases) - len(failed_ids),
        "failed_ids": failed_ids,
        "production_e2e_pass": False,
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# StreamingCorrector 集成 probe",
        "",
        f"- 状态：{report['status']}",
        f"- 工厂：`{report.get('factory', 'corrector_from_args')}`。源码没有 `create_streaming_corrector`。",
        f"- 端点：`{report.get('endpoint', ENDPOINT)}`",
        "- 没有读取或修改用户配置，没有麦克风，没有输入法，没有向其他应用输入。",
        "- jev 只是这一条热词。结果不能外推成全部错别字或整句听写准确率。",
        "",
    ]
    if report["status"] != "executed":
        lines.append(f"未执行原因：{report.get('warmup', {})}")
        lines.append("")
        lines.append("计划用例：工具 jeff→jev、结构保护下的人名 Jeff 不改、带软件上下文的人名句、中文数字和网址的本机规范化、0.05 秒超时保留原文。五条一起计数。")
        return "\n".join(lines) + "\n"
    lines.append(
        f"- 通过 {report.get('cases_passed')}/{report.get('cases_total')}。"
        f"失败：{report.get('failed_ids')}。"
    )
    lines.append("- 五条一起计数。基础合同通过不能拿来掩盖语义失败。")
    lines.append("- 生产端到端：未通过。这次没有录音，也没有输入法上屏。")
    lines.append("")
    for case in report["cases"]:
        lines.append(f"## {case['id']}")
        lines.append("")
        lines.append(f"- 输入：`{case['input']}`")
        lines.append(f"- 预期：`{case['expected']}`")
        lines.append(f"- 输出：`{case.get('output')}`")
        if "partial_output" in case:
            lines.append(f"- partial：`{case['partial_output']}`（{case['partial_ms']} ms）")
            lines.append(f"- poll：`{case['poll_output']}`（{case['poll_ms']} ms）")
        lines.append(f"- 耗时：{case.get('elapsed_ms')} ms")
        if "observation_wait_ms" in case:
            lines.append(
                f"- 观测等待：{case.get('observation_wait_ms')} ms（idle={case.get('observation_idle')}，不计入上面的 finish 耗时）"
            )
        lines.append(f"- 通过：{case['passed']}")
        posts = case.get("http", {}).get("posts", [])
        if posts:
            lines.append(f"- HTTP：{json.dumps(posts, ensure_ascii=False)}")
        else:
            lines.append("- HTTP：无")
        if case.get("note"):
            lines.append(f"- 说明：{case['note']}")
        lines.append("")
    return "\n".join(lines)


def write_report(report: dict[str, Any], json_path: Path, md_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="StreamingCorrector integration probe")
    parser.add_argument("--source-root", type=Path, default=DEFAULT_SOURCE_ROOT)
    parser.add_argument("--endpoint", default=ENDPOINT)
    parser.add_argument(
        "--warmup-report",
        type=Path,
        default=Path.cwd() / "reports" / "decider-warmup-final.json",
    )
    parser.add_argument(
        "--report-json",
        type=Path,
        default=Path.cwd() / "reports" / "streaming-decider-probe.json",
    )
    parser.add_argument(
        "--report-md",
        type=Path,
        default=Path.cwd() / "reports" / "streaming-decider-probe.md",
    )
    parser.add_argument("--wait-ready-s", type=float, default=WAIT_READY_S)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source_root = args.source_root.expanduser().resolve()
    if not (source_root / "recordian" / "streaming_correction.py").is_file():
        print(f"source root has no recordian.streaming_correction: {source_root}", file=sys.stderr)
        return 2
    sys.path.insert(0, str(source_root))
    warmup_path = args.warmup_report.expanduser().resolve()
    report_json = args.report_json.expanduser().resolve()
    report_md = args.report_md.expanduser().resolve()
    warmup = wait_until_ready(warmup_path, args.wait_ready_s)
    base = {
        "generated_by": "server/decision_trial/integration_probe.py",
        "source_root": str(source_root),
        "endpoint": args.endpoint,
        "warmup_path": str(warmup_path),
        "warmup": warmup,
        "user_config_read": False,
        "user_config_written": False,
        "microphone_used": False,
        "ime_used": False,
        "other_app_input": False,
        "real_recording": False,
        "factory": "recordian.streaming_correction.corrector_from_args",
    }
    if not warmup["ready"]:
        base.update(
            {
                "status": "prepared_not_executed",
                "create_streaming_corrector_present": False,
                "production_e2e_pass": False,
                "cases_total": 5,
                "cases_passed": 0,
                "failed_ids": [],
                "cases_planned": [
                    "tool_jeff_to_jev",
                    "person_jeff_unchanged",
                    "person_jeff_with_software_context",
                    "local_number_and_url",
                    "timeout_keeps_original",
                ],
            }
        )
        write_report(base, report_json, report_md)
        return 2
    try:
        result = run_probe(args.endpoint)
    except Exception as exc:
        base.update(
            {
                "status": "executed_error",
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(limit=8),
                "production_e2e_pass": False,
                "cases_total": 5,
                "cases_passed": 0,
                "failed_ids": [],
            }
        )
        write_report(base, report_json, report_md)
        return 1
    result.update(base)
    result["status"] = "executed"
    result["endpoint"] = args.endpoint
    write_report(result, report_json, report_md)
    return 0 if not result["failed_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
