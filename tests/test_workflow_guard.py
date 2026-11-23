"""静态守护：CI workflow 不得吞红，且 PR 必须取消过期运行。"""

import re
from pathlib import Path

import pytest

GITHUB = Path(__file__).resolve().parent.parent / ".github"
WORKFLOWS = sorted(p for pattern in ("*.yml", "*.yaml") for p in (GITHUB / "workflows").glob(pattern))
# 复合 action（若有）里同样不许吞红；.yml 与 .yaml 都要扫
CHECKED = WORKFLOWS + sorted(
    p for pattern in ("action.yml", "action.yaml") for p in (GITHUB / "actions").glob(f"*/{pattern}")
)

# 任何让失败变绿的写法；确需例外时在此登记（文件名 -> 原因），不要在 workflow 里就地放行。
# || true / || : / || exit 0 覆盖吞红的等价写法；匹配在续行合并后的逻辑行上进行。
SWALLOW = re.compile(
    r"continue-on-error\s*:|\|\|\s*(?:true\b|:|exit\s+0\b)|\bset\s+\+e\b|fail_ci_if_error\s*:\s*false",
    re.IGNORECASE,
)
ALLOWED: dict[str, str] = {}


def logical_lines(text: str) -> list[tuple[int, str]]:
    """合并续行为逻辑行，返回 (起始物理行号, 合并后内容)。

    行尾 ``\\`` 是显式续行；行尾 ``||`` 时 shell 把换行当续行（``||`` 换行 ``true`` 同样吞红），
    两者都并入下一行后再匹配。
    """
    out: list[tuple[int, str]] = []
    start, buf = 1, []
    for n, line in enumerate(text.splitlines(), 1):
        if not buf:
            start = n
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buf.append(stripped[:-1])
            continue
        buf.append(line)
        if stripped.endswith("||"):
            continue
        out.append((start, " ".join(part.rstrip() for part in buf)))
        buf = []
    if buf:  # 文件末尾的悬空续行
        out.append((start, " ".join(part.rstrip() for part in buf)))
    return out


def test_workflows_are_discovered() -> None:
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "native.yml", "release.yml"}


@pytest.mark.parametrize("path", CHECKED, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_workflow_does_not_swallow_failures(path: Path) -> None:
    if path.name in ALLOWED:
        pytest.skip(ALLOWED[path.name])
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in logical_lines(path.read_text(encoding="utf-8"))
        if not line.lstrip().startswith("#") and SWALLOW.search(line)
    ]
    assert not hits, "workflow swallows failures:\n" + "\n".join(hits)


@pytest.mark.parametrize("name", ["ci.yml", "native.yml"])
def test_pr_checks_cancel_superseded_runs_but_push_never_cancelled(name: str) -> None:
    text = (WORKFLOWS[0].parent / name).read_text(encoding="utf-8")
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    assert "github.sha" in text  # push 按 sha 分组，不被后续 push 顶掉
