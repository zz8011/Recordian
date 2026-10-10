# Rust runtime delivery plan

Implement inline with independent workers for the persistent D-Bus adapter and server buffer integration. The parent owns the Rust audio ABI, Python loader, packaging, final review, remote merge and deployment. This document records implementation and acceptance design.

The production crate is `native/recordian-core`, exposing a versioned C ABI. Tests precede implementation for audio edge equivalence, raw RMS, PCM decoding and bounded buffering. `src/recordian/native_core.py` loads the installed package library and reports selected backend; audio callers retain their pure Python fallback. Build/release tooling installs the library atomically and packaging includes it in platform-specific wheels.

The D-Bus worker owns `native/recordian-core/src/dbus*.rs`, `src/recordian/native_bus.py`, transport integration in `linux_commit.py` and their focused tests. Pin each BeginSession token to a unique bus owner, validate reply signatures and reject NUL/invalid arguments. No uncertain mutation can be retried through busctl. Use a private fixture for protocol and owner-loss tests.

The server worker owns server buffering changes and their tests. Use the shared audio ABI to decode buffered PCM, preserve 160 ms chunking and first/tail budgets, skip submissions with no ready chunk, and retain executor drain semantics. No automatic model load or live GPU run by a worker.

After integration run Cargo tests/Clippy, Python tests and relevant lint/build gates. Run the real model sequentially on public short/long PCM and verify full text/tail/normal close. Exercise input on a dedicated test window. An independent review checks FFI lifetime, errors, stale/uncertain sessions, bounds and fallback. Fix material findings before publishing.

Publish the branch and merge main, install the built artifact and restart desktop/ASR services with native mode required. Check loaded mappings and successful post-restart requests, then archive the original evidence and remove the completed Rust worktree. Preserve unrelated dirty files, unmerged worktrees and stashes. Save rollback instructions and report exact remote commit and runtime proof.
