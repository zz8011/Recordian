"""Test-only app: the real warmup path with a deliberately invalid second case.

Used once by the trial to prove that a *failing* warmup keeps uvicorn from ever binding the
port (`Application startup failed. Exiting.`, health never 200) instead of advertising a
half-initialised server.  Case 1 is the real short warmup case, so the failure is raised
after a real forward has already run; case 2 asks for a choice with a one-entry criteria map,
which the official ``decider.systemone.render_question`` rejects with
``ValueError: choice criteria: a map of 2..255 options`` -- no monkeypatching involved.
"""

import serve_warmup as W

W.CASES = [
    W.CASES[0],
    {
        "id": "selftest-invalid-criteria",
        "surface": "jeff",
        "heard": "jeff",
        "context": "",
        "focus": "先用jeff把依赖装好",
        "criteria": {"tool": "这里指的是软件工具、程序或插件。"},
    },
]
app = W.app
