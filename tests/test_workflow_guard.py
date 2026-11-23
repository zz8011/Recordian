"""静态守护：CI workflow 不得吞红，且 PR 必须取消过期运行。"""
import re
from pathlib import Path

import pytest

WORKFLOWS = sorted((Path(__file__).resolve().parent.parent / ".github" / "workflows").glob("*.yml"))

# 任何让失败变绿的写法；确需例外时在此登记（文件名 -> 原因），不要在 workflow 里就地放行。
SWALLOW = re.compile(r"continue-on-error\s*:|\|\|\s*true\b|\bset\s+\+e\b|fail_ci_if_error\s*:\s*false", re.IGNORECASE)
ALLOWED: dict[str, str] = {}


def test_workflows_are_discovered() -> None:
    assert {p.name for p in WORKFLOWS} >= {"ci.yml", "native.yml", "release.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_does_not_swallow_failures(path: Path) -> None:
    if path.name in ALLOWED:
        pytest.skip(ALLOWED[path.name])
    hits = [
        f"{path.name}:{n}: {line.strip()}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if not line.lstrip().startswith("#") and SWALLOW.search(line)
    ]
    assert not hits, "workflow swallows failures:\n" + "\n".join(hits)


@pytest.mark.parametrize("name", ["ci.yml", "native.yml"])
def test_pr_checks_cancel_superseded_runs_but_push_never_cancelled(name: str) -> None:
    text = (WORKFLOWS[0].parent / name).read_text(encoding="utf-8")
    assert "cancel-in-progress: ${{ github.event_name == 'pull_request' }}" in text
    assert "github.sha" in text  # push 按 sha 分组，不被后续 push 顶掉
