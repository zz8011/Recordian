"""静态守护：CI workflow 不得吞红（白名单式 shell 结构解析），且 PR 必须取消过期运行。

黑名单正则永远追不上 shell 的写法（``; true``、``|| (exit 0)``、``|| { :; }`` 都绕过过旧版），
因此改为白名单：PyYAML 读出每个 ``run:`` 步骤，逐字符扫描（感知引号/续行/heredoc），
只放行不会吞退出码的形式，其余一律判红并指出 file/job/step。
"""

import re
from pathlib import Path

import pytest
import yaml

GITHUB = Path(__file__).resolve().parent.parent / ".github"
WORKFLOWS = sorted(p for pattern in ("*.yml", "*.yaml") for p in (GITHUB / "workflows").glob(pattern))
# 复合 action（若有）里同样不许吞红；.yml 与 .yaml 都要扫
CHECKED = WORKFLOWS + sorted(
    p for pattern in ("action.yml", "action.yaml") for p in (GITHUB / "actions").glob(f"*/{pattern}")
)

# 显式例外（仓库根相对路径, job id, step 名）-> 必须给出理由。
# 当前所有 run: 步骤都已写成白名单形式，例外数为 0；新增例外必须给出理由并同步调大 ALLOWED_CAP。
ALLOWED: dict[tuple[str, str, str], str] = {}
ALLOWED_CAP = 0

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_RED_COMMANDS = {"true", ":"}  # 恒成功，可吞掉任何失败
_RED_KEYWORDS = {"if", "while", "until", "for", "case", "select", "function", "coproc", "time", "{", "}"}


class _ShellAudit:
    """白名单式扫描一段 run 脚本，返回违规描述列表（含脚本内相对行号）。

    放行：简单命令、前缀赋值、换行/``&&`` 分隔、``|``（仅 allow_pipe，即 bash 的 pipefail）、
    重定向（``>`` ``>>`` ``<&`` ``>&`` ``>|`` fd 前缀）、heredoc（正文不当命令解析）、
    ``$(...)``（内部递归套用同一套白名单）、引号/续行/注释。
    判红：``||``、``;``、``&``、分组/子 shell、``!``、复合关键字、``set +e``、``trap``、
    ``exit 0``、``true``/``:``、反引号、未闭合结构。
    """

    def __init__(self, script: str, allow_pipe: bool, context: str = "") -> None:
        self.s = script
        self.allow_pipe = allow_pipe
        self.context = context
        self.i = 0
        self.line = 1
        self.problems: list[str] = []

    def error(self, msg: str, line: int | None = None) -> None:
        self.problems.append(f"run 第 {line or self.line} 行{self.context}: {msg}")

    def run(self) -> list[str]:
        self._commands()
        return self.problems

    def _commands(self) -> None:
        words: list[tuple[str, int]] = []  # (词值, 词起始行)
        started = False  # 命令词已出现，其后的纯数字词不再是 fd 前缀
        want_target = False  # 上一 token 是重定向，等待目标词
        heredocs: list[tuple[str, bool]] = []  # (定界符, 是否 <<-)
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch == "\n":
                self.line += 1
                self.i += 1
                if heredocs:
                    self._skip_heredocs(heredocs)
                    heredocs = []
                if want_target:
                    self.error("重定向缺少目标")
                    want_target = False
                self._finish(words)
                words, started = [], False
                continue
            if ch in " \t":
                self.i += 1
                continue
            if ch == "#":
                self._skip_to_eol()
                continue
            if ch in "|&;()<>":
                op = self._operator()
                if op in ("<<", "<<-"):
                    heredocs.append(self._heredoc_delim(op == "<<-"))
                elif op in (">", ">>", ">&", ">|", "<", "<&"):
                    while not started and words and words[-1][0].isdigit():
                        words.pop()  # fd 前缀，如 2>err
                    want_target = True
                else:
                    if want_target:
                        self.error(f"重定向后接 {op!r}")
                        want_target = False
                    if op == "&&":
                        pass
                    elif op == "|":
                        if not self.allow_pipe:
                            self.error("管道：该 shell 无 pipefail，左侧退出码被吞")
                    else:  # || ; & ;; ( ) <
                        self.error(f"吞红写法 {op!r}")
                    self._finish(words)
                    words, started = [], False
                continue
            word, word_line = self._word()
            if want_target:
                want_target = False
                continue
            if not started and _ASSIGN.match(word):
                continue  # 命令前缀赋值
            words.append((word, word_line))
            started = True
        if want_target:
            self.error("重定向缺少目标")
        self._finish(words)

    def _finish(self, words: list[tuple[str, int]]) -> None:
        rest = list(words)
        while rest and _ASSIGN.match(rest[0][0]):
            rest.pop(0)
        if not rest:
            return
        cmd, line = rest[0]
        args = [w for w, _ in rest[1:]]
        if cmd == "!":
            self.error("! 取反使失败变成功", line)
        elif cmd in _RED_COMMANDS:
            self.error(f"命令 {cmd!r} 恒成功", line)
        elif cmd in _RED_KEYWORDS:
            self.error(f"复合结构关键字 {cmd!r} 不在白名单", line)
        elif cmd == "exit" and args == ["0"]:
            self.error("exit 0 强行变绿", line)
        elif cmd == "trap":
            self.error("trap 可改写退出码", line)
        elif cmd == "set" and ("+e" in args or ("+o" in args and "pipefail" in args)):
            self.error("set 关闭了 -e/pipefail", line)

    def _operator(self) -> str:
        two = self.s[self.i : self.i + 2]
        if two in ("||", "&&", ">>", ">&", ">|", "<&", ";;"):
            self.i += 2
            return two
        if two == "<<":
            third = self.s[self.i + 2 : self.i + 3]
            self.i += 3 if third == "-" else 2
            if third == "<":
                self.error("<<< herestring 不在白名单")
                return "<<"
            return "<<-" if third == "-" else "<<"
        ch = self.s[self.i]
        self.i += 1
        return ch

    def _word(self) -> tuple[str, int]:
        start_line = self.line
        buf: list[str] = []
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch in " \t\n|&;()<>":
                break
            if ch == "'":
                self._quoted("'", buf)
            elif ch == '"':
                self._double_quoted(buf)
            elif ch == "\\":
                nxt = self.s[self.i + 1] if self.i + 1 < len(self.s) else "\n"
                if nxt != "\n":
                    buf.append(nxt)
                else:
                    self.line += 1  # 续行
                self.i += 2
            elif ch == "$":
                self._dollar(buf)
            elif ch == "`":
                self._backtick()
            else:
                buf.append(ch)
                self.i += 1
        return "".join(buf), start_line

    def _quoted(self, quote: str, buf: list[str]) -> None:
        self.i += 1
        while self.i < len(self.s) and self.s[self.i] != quote:
            if self.s[self.i] == "\n":
                self.line += 1
            buf.append(self.s[self.i])
            self.i += 1
        self.i += 1  # 闭引号（缺失则到 EOF，词被截断但无害）

    def _double_quoted(self, buf: list[str]) -> None:
        self.i += 1
        while self.i < len(self.s) and self.s[self.i] != '"':
            ch = self.s[self.i]
            if ch == "\\" and self.i + 1 < len(self.s):
                nxt = self.s[self.i + 1]
                if nxt != "\n":
                    buf.append(nxt)
                else:
                    self.line += 1
                self.i += 2
            elif ch == "$":
                self._dollar(buf)
            elif ch == "`":
                self._backtick()
            else:
                if ch == "\n":
                    self.line += 1
                buf.append(ch)
                self.i += 1
        self.i += 1

    def _dollar(self, buf: list[str]) -> None:
        nxt = self.s[self.i + 1] if self.i + 1 < len(self.s) else ""
        if nxt == "(":
            inner = self._paren_body()
            sub = _ShellAudit(inner, self.allow_pipe, context="$(...) 内")
            self.problems.extend(sub.run())
            buf.append("$(...)")
        elif nxt == "{":
            self._param_expansion(buf)
        else:
            buf.append("$")
            self.i += 1

    def _paren_body(self) -> str:
        self.i += 2  # 消费 $(
        depth = 1
        out: list[str] = []
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch in "'\"":
                self._consume_quoted_raw(ch, out)
            elif ch == "\\" and self.i + 1 < len(self.s):
                out.extend(self.s[self.i : self.i + 2])
                self.i += 2
            elif ch == "(":
                depth += 1
                out.append(ch)
                self.i += 1
            elif ch == ")":
                depth -= 1
                self.i += 1
                if depth == 0:
                    return "".join(out)
                out.append(ch)
            else:
                if ch == "\n":
                    self.line += 1
                out.append(ch)
                self.i += 1
        self.error("未闭合的 $()")
        return "".join(out)

    def _consume_quoted_raw(self, quote: str, out: list[str]) -> None:
        out.append(quote)
        self.i += 1
        while self.i < len(self.s) and self.s[self.i] != quote:
            if self.s[self.i] == "\n":
                self.line += 1
            out.append(self.s[self.i])
            self.i += 1
        out.append(quote)
        self.i += 1

    def _param_expansion(self, buf: list[str]) -> None:
        start_line = self.line
        self.i += 2
        depth = 1
        while self.i < len(self.s):
            ch = self.s[self.i]
            if ch in "'\"":
                self._consume_quoted_raw(ch, [])
            elif ch == "\\" and self.i + 1 < len(self.s):
                self.i += 2
            elif self.s.startswith("${", self.i):
                depth += 1
                self.i += 2
            elif ch == "}":
                depth -= 1
                self.i += 1
                if depth == 0:
                    buf.append("${...}")
                    return
            else:
                if ch == "\n":
                    self.line += 1
                self.i += 1
        self.line = start_line
        self.error("未闭合的 ${...}")

    def _backtick(self) -> None:
        self.error("反引号命令替换不在白名单（改用 $()）")
        self.i += 1
        while self.i < len(self.s) and self.s[self.i] != "`":
            if self.s[self.i] == "\n":
                self.line += 1
            self.i += 1
        self.i += 1

    def _heredoc_delim(self, strip_tabs: bool) -> tuple[str, bool]:
        while self.i < len(self.s) and self.s[self.i] in " \t":
            self.i += 1
        delim = ""
        while self.i < len(self.s) and self.s[self.i] not in " \t\n|&;()<>":
            delim += self.s[self.i]
            self.i += 1
        if len(delim) >= 2 and delim[0] == delim[-1] and delim[0] in "'\"":
            delim = delim[1:-1]
        if not delim:
            self.error("heredoc 定界符为空")
        return delim, strip_tabs

    def _skip_heredocs(self, delims: list[tuple[str, bool]]) -> None:
        for delim, strip_tabs in delims:
            found = False
            while self.i < len(self.s):
                eol = self.s.find("\n", self.i)
                body = self.s[self.i :] if eol < 0 else self.s[self.i : eol]
                self.i = len(self.s) if eol < 0 else eol + 1
                self.line += 1
                if (body.lstrip("\t") if strip_tabs else body) == delim:
                    found = True
                    break
            if not found:
                self.error(f"heredoc {delim!r} 未闭合")

    def _skip_to_eol(self) -> None:
        while self.i < len(self.s) and self.s[self.i] != "\n":
            self.i += 1


def _step_label(step: dict) -> str:
    return str(step.get("name") or step.get("uses") or step.get("id") or "<unnamed>")


def _effective_shell(step: dict, job: dict | None, doc: dict) -> str:
    """返回 'bash' / 'sh' / 其他原样字符串（未审计）。GitHub 默认 bash -eo pipefail。"""
    raw = step.get("shell")
    if not raw:
        for scope in (job, doc):
            defaults = scope.get("defaults") if isinstance(scope, dict) else None
            run_defaults = defaults.get("run") if isinstance(defaults, dict) else None
            if isinstance(run_defaults, dict) and run_defaults.get("shell"):
                raw = run_defaults["shell"]
                break
    if not raw:
        runs_on = str((job or {}).get("runs-on") or "")
        return "pwsh" if runs_on.startswith("windows") else "bash"
    raw = str(raw).strip()
    # 带参数的自定义串（如 "bash {0}"）丢掉了 -e/pipefail 语义，同样视为未审计
    return {"bash": "bash", "sh": "sh"}.get(raw, raw)


def workflow_violations(path: Path) -> list[tuple[str, str, str]]:
    """结构化解析 workflow/复合 action，返回 (job, step, 违规描述) 列表。"""
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict):
        return [("?", "?", "YAML 顶层不是映射")]
    problems: list[tuple[str, str, str]] = []
    steps: list[tuple[str, dict | None, dict]] = []
    jobs = doc.get("jobs")
    if isinstance(jobs, dict):
        for job_id, job in jobs.items():
            if not isinstance(job, dict):
                continue
            if "continue-on-error" in job:
                problems.append((str(job_id), "<job>", "job 声明 continue-on-error"))
            steps.extend((str(job_id), job, st) for st in (job.get("steps") or []) if isinstance(st, dict))
    runs = doc.get("runs")  # 复合 action
    if isinstance(runs, dict):
        steps.extend(("action", None, st) for st in (runs.get("steps") or []) if isinstance(st, dict))
    for job_id, job, step in steps:
        label = _step_label(step)
        if "continue-on-error" in step:
            problems.append((job_id, label, "step 声明 continue-on-error"))
        with_ = step.get("with")
        if isinstance(with_, dict) and with_.get("fail_ci_if_error") is False:
            problems.append((job_id, label, "fail_ci_if_error: false 会让覆盖率为红也不失败"))
        run = step.get("run")
        if not isinstance(run, str) or not run.strip():
            continue
        shell = _effective_shell(step, job, doc)
        if shell == "bash":
            msgs = _ShellAudit(run, allow_pipe=True).run()
        elif shell == "sh":
            msgs = _ShellAudit(run, allow_pipe=False).run()
        else:
            msgs = [f"shell {shell!r} 不在白名单（bash/sh 之外未审计），如确需使用请登记例外并给理由"]
        problems.extend((job_id, label, m) for m in msgs)
    return problems


def test_workflows_are_discovered() -> None:
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "native.yml", "release.yml"}


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_workflow_does_not_swallow_failures(path: Path) -> None:
    excluded = {(job, step) for rel, job, step in ALLOWED if (GITHUB.parent / rel) == path}
    hits = [
        f"{path.name} [{job}/{step}]: {msg}"
        for job, step, msg in workflow_violations(path)
        if (job, step) not in excluded
    ]
    assert not hits, "workflow 吞红:\n" + "\n".join(hits)


@pytest.mark.parametrize("name", ["ci.yml", "native.yml"])
def test_pr_checks_cancel_superseded_runs_but_push_never_cancelled(name: str) -> None:
    text = (WORKFLOWS[0].parent / name).read_text(encoding="utf-8")
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    assert "github.sha" in text  # push 按 sha 分组，不被后续 push 顶掉


def test_permissions_stay_read_only() -> None:
    # 未声明的 workflow 用 token 默认权限；一旦声明就必须最小化为 read
    for path in WORKFLOWS:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        perms = doc.get("permissions") if isinstance(doc, dict) else None
        if perms is None:
            continue
        assert isinstance(perms, dict) and set(perms.values()) <= {"read"}, f"{path.name}: {perms}"


def test_exception_list_is_minimal_and_live() -> None:
    # 例外清单不得增长到超过当前真实需要：数量封顶，且每条都必须命中一个真实存在的判红步骤
    assert len(ALLOWED) <= ALLOWED_CAP, "例外数超过 ALLOWED_CAP，请先确认无法改写成白名单写法"
    for (rel, job_id, step_name), reason in ALLOWED.items():
        assert reason.strip(), f"{rel}[{job_id}/{step_name}] 缺少理由"
        path = GITHUB.parent / rel
        assert path in CHECKED, f"例外指向未扫描的文件 {rel}"
        live = {(job, label) for job, label, _ in workflow_violations(path)}
        assert (job_id, step_name) in live, f"例外 {rel}[{job_id}/{step_name}] 已失效（该步骤不再判红）"


def _violations_of_run(tmp_path: Path, run: str, shell: str | None = None) -> list[str]:
    step = {"name": "s", "run": run}
    if shell:
        step["shell"] = shell
    doc = {"jobs": {"j": {"runs-on": "ubuntu-latest", "steps": [step]}}}
    path = tmp_path / "wf.yml"
    path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return [msg for _, _, msg in workflow_violations(path)]


# 每种判红写法至少一个用例；旧版黑名单被实测绕过的三种写法必须在内
RED_RUNS = [
    "pip install -e .; true",
    "make || (exit 0)",
    "make || { :; }",
    "make || true",
    "make || :",
    "make || exit 0",
    "make ||\ntrue",  # 行尾 || 续行
    "set +e\nmake",
    "set +o pipefail\nmake",
    "trap '' ERR\nmake",
    "make &",
    "x=$(make || true)",
    "! pytest",
    "if pytest; then echo ok; fi",
    "(exit 0)",
    "{ :; }",
    "exit 0",
    "true",
    ":",
    'for f in *.c; do cc "$f"; done',
    "make;make2",  # 无空格分号
    "make 2>&1 &",  # & 前有重定向
    "echo `ls`",
    "case x in x) true;; esac",
]


@pytest.mark.parametrize("run", RED_RUNS)
def test_run_snippet_is_red(tmp_path: Path, run: str) -> None:
    assert _violations_of_run(tmp_path, run), f"应当判红: {run!r}"


# 白名单形式必须放行（防误报）；含真实 workflow 用到的全部形态
GREEN_RUNS = [
    "python -m pip install -e '.[dev]'",
    "ruff check src/ tests/ && mypy src/recordian",
    'mkdir -p "$RUNNER_TEMP/bin"\nln -s x y\necho p >> $GITHUB_PATH',
    "sudo apt-get install -y \\\n  libglib2.0-dev pkg-config",
    "cat foo | grep bar",  # bash 默认 -eo pipefail，管道安全
    "FOO=1 BAR=2 python -m build",
    "python -c 'import a; print(b)'",  # 引号内的 ; 不是分隔符
    'echo "a || b ; c & d"',
    "python - <<'PY'\nimport os\nx = 'a || b ; c'\nPY",  # heredoc 正文不解析
    "cat <<EOF\nhi || there\nEOF",
    'echo "VERSION=${GITHUB_REF#refs/tags/v}" >> $GITHUB_OUTPUT',  # ${} 里的 # 不是注释
    "ln -s \"$(python -c 'import a; print(b)')\" target",
    "# 注释里的 ; || & ( ) { } 全部无害",
    "exit 1",
    "make > out.log 2>&1",
    "make 2> err.log",
    "set -e\nmake",
    "echo Done. \\; really",
    "cd dist\nsha256sum * > SHA256SUMS.txt\ncat SHA256SUMS.txt",
    "pytest -q tests/",
]


@pytest.mark.parametrize("run", GREEN_RUNS)
def test_run_snippet_is_green(tmp_path: Path, run: str) -> None:
    assert not _violations_of_run(tmp_path, run), f"误报: {run!r}"


def test_sh_shell_rejects_pipes_but_allows_rest(tmp_path: Path) -> None:
    assert _violations_of_run(tmp_path, "a | b", shell="sh")
    assert not _violations_of_run(tmp_path, "make && ls -l", shell="sh")


def test_unaudited_shell_is_red_until_whitelisted(tmp_path: Path) -> None:
    # python/pwsh/cmd/带参数自定义串不在审计范围，需显式例外
    assert _violations_of_run(tmp_path, "print('hi')", shell="python")
    assert _violations_of_run(tmp_path, "Write-Host hi", shell="pwsh")
    assert _violations_of_run(tmp_path, "make", shell="bash {0}")
    assert not _violations_of_run(tmp_path, "make", shell="bash")
    assert not _violations_of_run(tmp_path, "make")  # 未声明 → GitHub 默认 bash -eo pipefail
