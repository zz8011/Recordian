# Recordian — Project Index

> Voice dictation for Linux. Audio → ASR → hotword correction → input-method commit, with optional text refinement.

**Last updated:** 2026-10-10

> 文档导航见 [`docs/README.md`](docs/README.md)；版本历史见 [`CHANGELOG.md`](CHANGELOG.md)。

## Module Map

| Directory | Purpose | Notes |
|----------|---------|-------|
| `src/recordian/` | Core application source | Entrypoint: `cli.py` |
| `src/recordian/providers/` | ASR providers & text refiners | **Start here for ASR changes** |
| `src/recordian/providers/asr/` | ASR backend implementations | qwen, streaming, etc. |
| `src/recordian/providers/refine/` | Text refinement LLMs | cloud LLM refine pipeline |
| `server/` | Local ASR server processes | `confucius_streaming_server.py` (WebSocket v1, loopback), `qwen_streaming_server.py` (HTTP) |
| `tests/` | Unit & integration tests | |
| `native/recordian-core/` | Dependency-free Rust cdylib + rlib, C ABI1 | Audio conversion/RMS, bounded PCM FIFO, persistent GIO D-Bus transport |
| `src/recordian/_native/` | Generated package-local shared library + SHA256 metadata | Gitignored; build before startup |
| `docs/` | 用户手册、故障排查、功能专题 | 索引见 `docs/README.md`；历史文档在 `docs/archive/` |
| `models/` | ASR model files (gitignored) | Do not commit large files |
| `presets/` | 文本精炼 prompt 预设（`.md`） | 文件名即 preset 名 |
| `examples/` | Usage examples | |
| `assets/` | Static assets | |

## Canonical Files

| File | Purpose |
|------|---------|
| `cli.py` | CLI entrypoint (`recordian` command) — single-shot `--wav` recognition, no `--config-path` |
| `hotkey_dictate.py` | Hotkey daemon (`recordian-hotkey-dictate`); loads JSON config via `--config-path` |
| `runtime_config.py` | JSON runtime-config normalization (e.g. `~/.config/recordian/hotkey.json`) |
| `config.py` | Versioned app-config dataclasses + validator (not the hotkey config loader) |
| `engine.py` | Core dictation engine |
| `providers/qwen_asr.py` | Primary ASR provider |
| `providers/confucius_asr.py` | Confucius4-R2T2 streaming provider (WebSocket client, v1 protocol) |
| `server/confucius_streaming_server.py` | Local single-user Confucius streaming server (see `server/README-confucius.md`) |
| `hotword_corrector.py` | Deterministic hotword correction (常用词/asr_context + `错词→正词` + ASCII/拼音), runs ASR → refine 之间 |
| `continuous_dictation.py` / `duration_guard.py` | One microphone and IME token across sample-counted Confucius segments; bounded raw tail and failure handling |
| `streaming_correction.py` / `semif_judge.py` / `jev_judge.py` | Optional asynchronous candidate judgment via SemIf or official Jev, contextual aliases, and shared corrector factory |
| `spoken_formatting.py` / `text_cleanup.py` | Spoken numbers and URL dots, with literal-text protection |
| `auto_lexicon.py` | Auto-learned hotword lexicon (fragment-filtered, separate auto quota) |
| `voice_wake.py` | Wake-word activation |
| `postprocess_pipeline.py` | Text refinement pipeline |
| `tray_app.py` | TrayApp class and `main()` entry point (tkinter overlay + menu wiring) |
| `tray_gui.py` | 兼容 shim；`recordian-tray` 的实际入口，转导出 `tray_app` 等拆分后的子模块 |
| `tray_utils.py` / `tray_menu.py` | 纯工具函数；AppIndicator 菜单构建与包内状态图标 |
| `tray_context_editor.py` / `tray_diagnostics.py` / `tray_speaker_wizard.py` | 词库编辑器、运行时诊断、声纹录入向导 |
| `native_settings.py` / `settings_draft.py` | GTK preferences, editable snapshot and safe save |
| `tray_settings.py` / `tray_settings_utils.py` | Legacy form helpers and settings-window compatibility layer |
| `recommended_profile.py` | Local Confucius profile, display labels, and endpoint validation |
| `waveform_renderer.py` | Recording overlay window + state machine (pyglet) |
| `orb_shader.py` | Liquid-glass voice orb GLSL shader (voiceWave preset, ported from LerSent001/orb, MIT) |
| `backend_manager.py` | Backend lifecycle management |
| `output_mute.py` | Desktop recording output mute and crash recovery; enabled by RECORDIAN_MUTE_OUTPUT |
| `agent_entry.py` / `agent_panel.py` / `agent_panel.html` | Agent voice routing, Hermes CLI/Gateway sessions, and local task panel |
| `agent_response_overlay.py` | Movable and resizable floating Agent replies; explicit close button or timeout dismisses without taking focus |
| `desktop_control.py` | Compositor control socket, recording toggle and status stream |
| `local_auth.py` / `http_service.py` / `audio_budget.py` | Private token/TLS, bounded HTTP transport and uploaded-audio validation |
| `pyproject.toml` | Python package config (uv/pip) |
| `scripts/build_native_core.py` | Offline release build, ABI1 validation and atomic library install |
| `setup.py` / `MANIFEST.in` | Optional build-time native wheel with platform tag; source-only Rust sdist |
| `native_core.py` / `native_bus.py` | Python adapters for Rust audio and persistent GIO transport; auto/required/python modes |

## Canonical Presets

预设是 `presets/` 下的 `.md` prompt 文件，**文件名（去掉扩展名）就是 preset 名**，由
`preset_manager.py` 的 `glob("*.md")` 加载，托盘菜单按此列表展示。

| File | Purpose |
|------|---------|
| `presets/default.md` | 默认：整理口语，去重去语气词（`refine_preset` 的默认值） |
| `presets/intent.md` | 意图整理：去掉对话废话和改口痕迹 |
| `presets/formal.md` / `summary.md` / `meeting.md` / `technical.md` | 书面语、摘要、会议纪要、技术文档风格 |
| `presets/code-comment.md` | 代码注释风格 |
| `presets/English.md` / `Japanese.md` / `Korean.md` / `Arabic.md` / `Indonesian.md` / `Uyghur.md` | 翻译类预设 |
| `presets/Extended.md` | 在原意基础上适度扩写 |

完整清单与格式说明见 `presets/README.md`。新增 `.md` 文件即可被托盘菜单识别。

## Where To Go

- **文档导航** → `docs/README.md`（用户手册、故障排查、功能专题的完整索引）
- **Rust runtime build/package/deployment** → `docs/RUST-RUNTIME.zh-CN.md` + `.github/workflows/native.yml`
- **历史文档** → `docs/archive/`（已归档的研究与过程记录，不再维护）
- **ASR provider changes** → `src/recordian/providers/INDEX.md`
- **Text refinement** → `src/recordian/providers/` + `postprocess_pipeline.py`
- **Hotkey configuration** → `hotkey_dictate.py` + `recordian-hotkey-dictate --help`
- **Continuous Alt dictation** → `continuous_dictation.py` + `docs/CONTINUOUS-DICTATION.zh-CN.md`
- **Local streaming ASR server** → `server/confucius_streaming_server.py` + `server/README-confucius.md`
- **Agent voice entry** → `agent_entry.py` + `docs/AGENT-VOICE.zh-CN.md` (Hermes implemented)
- **Wake word** → `voice_wake.py`; local setup and validation: `docs/VOICE-WAKE.zh-CN.md`
- **Config schema** → `runtime_config.py` + `pyproject.toml`
- **Running the daemon** → `recordian-hotkey-dictate --help`
- **Testing** → `pytest tests/ -v`

## Architecture (one-liner)

```
Audio capture (hotkey / wake word)
  → ASR (Confucius streaming / HTTP service / local Qwen)
  → hotword_corrector (常用词 + 显式替换 + ASCII/拼音)
  → Text refinement pipeline (cloud LLM or local, 热词纠错目标 + 保护)
  → Fcitx preedit + final commit, or configured non-streaming output backend
```

Streaming type-on-screen stays off by default (`enable_streaming_commit=false`). A true
incremental ASR path now exists: `asr_provider=confucius-asr` (providers/confucius_asr.py)
against the local server `server/confucius_streaming_server.py` — setup and the measured
limits in `server/README-confucius.md`; design/acceptance in `docs/STREAMING-IME-PLAN.zh-CN.md`.

## Key Design Decisions

- **ASR**: `qwen-asr` is the default provider; `streaming_base.py` for real-time streaming;
  `confucius-asr` and `http-cloud` are the alternatives (`runtime_config.ASR_PROVIDER_CHOICES`)
- **Text refine**: `cloud_llm_refiner.py` (HTTP LLM), `llamacpp_text_refiner.py` (local), or
  `qwen_text_refiner.py`; selected by `refine_provider` (`local` / `cloud` / `llamacpp`)
- **Contextual correction**: `jev` (default, official CLI on the LAN Plumb service) or `semif`
- **Wake word**: `voice_wake.py` — separate from hotkey mode
- **Tray**: `tray_gui.py` is the `recordian-tray` entry point (compat shim); the implementation
  lives in `tray_app.py` plus the focused `tray_*` submodules; `native_settings.py` for the GTK preferences window
- **Continuous capture**: Audio is assigned to bounded ASR sessions; committed input remains in the application. Correction context and in-memory history have explicit bounds.
- **Rust runtime**: C ABI1 handles audio conversion/RMS, bounded PCM FIFO and persistent GIO calls. Python UI/agents/correction and C++/CUDA inference remain; retain 160 ms scheduling (320 ms candidate rejected). Build only before use; require native explicitly for deployment, or select Python for rollback.

## Common Tasks

```bash
# Run the hotkey daemon using the saved settings
recordian-hotkey-dictate --config-path "$HOME/.config/recordian/hotkey.json"

# Run tray GUI
recordian-tray --config-path "$HOME/.config/recordian/hotkey.json"

# Run benchmarks（benchmark.py 只提供阈值判断，可执行入口在 performance_benchmark.py）
python -m recordian.performance_benchmark

# Type check
mypy src/recordian/
```

The hotkey daemon and tray are separate entry points from `recordian`, which
handles single-shot files. Configure optional voice wake and refinement in the
tray settings. A Confucius model service must be ready before dictation starts;
`recordian-tray` alone does not launch that service.

## Archived

Old configs and experimental modules may be in `src/recordian/` without a clear index. Do not use unless explicitly requested or found via grep + confirmation.
