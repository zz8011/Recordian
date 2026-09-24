# ASR Providers & Text Refiners

**Last updated:** 2026-07-02

## Provider Map

### ASR Providers

| File | Purpose | Status |
|------|---------|--------|
| `qwen_asr.py` | **Primary provider** — Qwen ASR (local transformers; preview-only partials, not true streaming) | Active |
| `confucius_asr.py` | Confucius4-R2T2 streaming ASR over WebSocket (`confucius-asr`) | Active — true incremental |
| `streaming_base.py` | Base class for real-time streaming ASR | Base class |
| `asr_context.py` | Context manager for ASR session state | Utility |

### Text Refiners

| File | Purpose | Status |
|------|---------|--------|
| `cloud_llm_refiner.py` | HTTP LLM refine (GLM / OpenAI compatible) | Active — main refine path |
| `llamacpp_text_refiner.py` | Local llama.cpp server refine | Alternative |
| `qwen_text_refiner.py` | Qwen-specific text refinement | Alternative |
| `base_text_refiner.py` | Base class for all text refiners | Base class |

### Infrastructure

| File | Purpose |
|------|---------|
| `http_cloud.py` | Shared HTTP client for cloud providers |
| `base.py` | Abstract base for all providers |

## Canonical Files

| File | Purpose |
|------|---------|
| `base.py` | `ASRProvider` / `TextRefiner` abstract interfaces |
| `qwen_asr.py` | **Default ASR** — use this for hotkey/wake mode |
| `cloud_llm_refiner.py` | **Default refiner** — use this for text refinement pipeline |
| `http_cloud.py` | HTTP provider base (`HttpASRProvider`, `HttpTTSProvider`) |

## Where To Go

- **Primary ASR** → `qwen_asr.py`
- **Primary refiner** → `cloud_llm_refiner.py`
- **Streaming/real-time ASR** → `streaming_base.py`
- **Adding a new ASR provider** → extend `base.ASRProvider`, follow `qwen_asr.py` pattern
- **Adding a new refiner** → extend `base_text_refiner.py`, follow `cloud_llm_refiner.py` pattern
- **HTTP provider issues** → `http_cloud.py`

## Provider Selection Logic

Actual selection happens in `linux_dictate.py::create_provider(args)` from the
`--asr-provider` CLI flag / `asr_provider` JSON config key (there is no
`config.yaml`; hotkey config is JSON loaded via `recordian-hotkey-dictate
--config-path`):

```
asr_provider: "qwen-asr" (default) → qwen_asr.py
asr_provider: "http-cloud"         → http_cloud.py
asr_provider: "confucius-asr"      → confucius_asr.py (WebSocket streaming;
                                     endpoint from asr_realtime_endpoint,
                                     auth token from asr_api_key)
```

The matching local server for `confucius-asr` is
`server/confucius_streaming_server.py` — setup in `server/README-confucius.md`.
Refine backend is selected by `--refine-provider {local,cloud,llamacpp}`
(`cloud_llm_refiner.py` / `qwen_text_refiner.py` / `llamacpp_text_refiner.py`).

## Key Interfaces

```python
# ASR Provider interface (base.py) — synchronous, no async API
class ASRProvider(ABC):
    def transcribe_file(self, wav_path: Path, *, hotwords: list[str]) -> ASRResult
    # realtime-capable providers (confucius_asr.py) additionally expose
    # start_realtime_session(*, hotwords) -> session with push_audio/push_pcm16/finish

# Text Refiner interface (base_text_refiner.py)
class TextRefiner(ABC):
    def refine(self, text: str) -> str
```

## Notes

- `asr_context.py` is NOT a provider — it's a context manager wrapping ASR calls
- Multiple refiners can be chained in `postprocess_pipeline.py`
- Provider credentials are set via environment variables, not hardcoded
