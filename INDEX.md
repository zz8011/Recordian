# Recordian — Project Index

> Voice dictation daemon for Linux. Records audio → ASR → text refinement → clipboard paste.

**Last updated:** 2026-07-20

## Module Map

| Directory | Purpose | Notes |
|----------|---------|-------|
| `src/recordian/` | Core application source | Entrypoint: `cli.py` |
| `src/recordian/providers/` | ASR providers & text refiners | **Start here for ASR changes** |
| `src/recordian/providers/asr/` | ASR backend implementations | qwen, streaming, etc. |
| `src/recordian/providers/refine/` | Text refinement LLMs | cloud LLM refine pipeline |
| `tests/` | Unit & integration tests | |
| `docs/` | Architecture & research docs | |
| `models/` | ASR model files (gitignored) | Do not commit large files |
| `presets/` | JSON preset configurations | |
| `examples/` | Usage examples | |
| `assets/` | Static assets | |

## Canonical Files

| File | Purpose |
|------|---------|
| `cli.py` | CLI entrypoint (`recordian` command) |
| `config.py` | Config loader (YAML schema) |
| `engine.py` | Core dictation engine |
| `providers/qwen_asr.py` | Primary ASR provider |
| `hotword_corrector.py` | Deterministic hotword correction (常用词/asr_context + `错词→正词` + ASCII/拼音), runs ASR → refine 之间 |
| `auto_lexicon.py` | Auto-learned hotword lexicon (fragment-filtered, separate auto quota) |
| `hotkey_dictate.py` | Hotkey-based dictation |
| `voice_wake.py` | Wake-word activation |
| `postprocess_pipeline.py` | Text refinement pipeline |
| `tray_app.py` | System tray GUI |
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
- **Hotkey configuration** → `hotkey_dictate.py` + `hotkey_dictate --help`
- **Wake word** → `voice_wake.py`
- **Config schema** → `config.py` + `pyproject.toml`
- **Running the daemon** → `cli.py --help`
- **Testing** → `pytest tests/ -v`

## Architecture (one-liner)

```
Audio capture (hotkey / wake word)
  → ASR (http-cloud / qwen_asr oneshot file transcription)
  → hotword_corrector (常用词 + 显式替换 + ASCII/拼音)
  → Text refinement pipeline (cloud LLM or local, 热词纠错目标 + 保护)
  → clipboard paste after key release
```

Streaming type-on-screen is gated off (`enable_streaming_commit=false`, empty `asr_realtime_endpoint`). Do not enable it until a true streaming ASR exists.

## Key Design Decisions

- **ASR**: qwen_asr is primary provider; `streaming_base.py` for real-time streaming
- **Text refine**: `cloud_llm_refiner.py` (HTTP LLM) or `llamacpp_text_refiner.py` (local)
- **Wake word**: `voice_wake.py` — separate from hotkey mode
- **Tray**: `tray_app.py` — system tray GUI; `tray_settings.py` for settings UI
- **No reset**: Conversation/transcript is never truncated. Memory stays continuous.

## Common Tasks

```bash
# Run dictation (hotkey mode)
python -m recordian.cli --mode hotkey

# Run with wake word
python -m recordian.cli --mode wake

# Run with LLM refinement
python -m recordian.cli --mode hotkey --refine

# Run tray GUI
python -m recordian.tray_app

# Run benchmarks
python -m recordian.benchmark

# Type check
mypy src/recordian/
```

## Archived

Old configs and experimental modules may be in `src/recordian/` without a clear index. Do not use unless explicitly requested or found via grep + confirmation.
