# Recordian — Project Index

> Voice dictation for Linux. Audio → ASR → hotword correction → input-method commit, with optional text refinement.

**Last updated:** 2026-09-25

## Module Map

| Directory | Purpose | Notes |
|----------|---------|-------|
| `src/recordian/` | Core application source | Entrypoint: `cli.py` |
| `src/recordian/providers/` | ASR providers & text refiners | **Start here for ASR changes** |
| `src/recordian/providers/asr/` | ASR backend implementations | qwen, streaming, etc. |
| `src/recordian/providers/refine/` | Text refinement LLMs | cloud LLM refine pipeline |
| `server/` | Local ASR server processes | `confucius_streaming_server.py` (WebSocket v1, loopback), `qwen_streaming_server.py` (HTTP) |
| `server/decision_trial/` | SystemOne model comparison and synthetic Chinese hotword corpus | Read its README for metric boundaries and reproducible runs |
| `server/decider_trial/` / `server/winnow_trial/` | Isolated AMD server trial deployment | Pinned models, startup checks, and rollback instructions |
| `tests/` | Unit & integration tests | |
| `docs/` | Architecture & research docs | |
| `models/` | ASR model files (gitignored) | Do not commit large files |
| `presets/` | JSON preset configurations | |
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
| `hotkey_dictate.py` | Hotkey-based dictation |
| `voice_wake.py` | Wake-word activation |
| `postprocess_pipeline.py` | Text refinement pipeline |
| `tray_app.py` | System tray GUI |
| `tray_settings.py` / `tray_menu.py` | Daily settings, advanced options, and tray actions |
| `recommended_profile.py` | Local Confucius profile, display labels, and endpoint validation |
| `waveform_renderer.py` | Recording overlay window + state machine (pyglet) |
| `orb_shader.py` | Liquid-glass voice orb GLSL shader (voiceWave preset, ported from LerSent001/orb, MIT) |
| `backend_manager.py` | Backend lifecycle management |
| `pyproject.toml` | Python package config (uv/pip) |

## Canonical Presets

| File | Purpose |
|------|---------|
| `presets/default.json` | Default dictation preset |
| `presets/refine.json` | With LLM text refinement |

## Where To Go

- **ASR provider changes** → `src/recordian/providers/INDEX.md`
- **Text refinement** → `src/recordian/providers/` + `postprocess_pipeline.py`
- **Hotkey configuration** → `hotkey_dictate.py` + `recordian-hotkey-dictate --help`
- **Continuous Alt dictation** → `continuous_dictation.py` + `docs/CONTINUOUS-DICTATION.zh-CN.md`
- **Local streaming ASR server** → `server/confucius_streaming_server.py` + `server/README-confucius.md`
- **Contextual correction model selection** → `docs/DECISION-MODEL-SELECTION.zh-CN.md` + `server/decision_trial/README.md`
- **Wake word** → `voice_wake.py`
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

- **ASR**: qwen_asr is primary provider; `streaming_base.py` for real-time streaming
- **Text refine**: `cloud_llm_refiner.py` (HTTP LLM) or `llamacpp_text_refiner.py` (local)
- **Wake word**: `voice_wake.py` — separate from hotkey mode
- **Tray**: `tray_app.py` — system tray GUI; `tray_settings.py` for settings UI
- **Continuous capture**: Audio is assigned to bounded ASR sessions; committed input remains in the application. Correction context and in-memory history have explicit bounds.

## Common Tasks

```bash
# Run the hotkey daemon using the saved settings
recordian-hotkey-dictate --config-path "$HOME/.config/recordian/hotkey.json"

# Run tray GUI
recordian-tray --config-path "$HOME/.config/recordian/hotkey.json"

# Run benchmarks
python -m recordian.benchmark

# Type check
mypy src/recordian/
```

The hotkey daemon and tray are separate entry points from `recordian`, which
handles single-shot files. Configure optional voice wake and refinement in the
tray settings. A Confucius model service must be ready before dictation starts;
`recordian-tray` alone does not launch that service.

## Archived

Old configs and experimental modules may be in `src/recordian/` without a clear index. Do not use unless explicitly requested or found via grep + confirmation.
