# ASR Providers & Text Refiners

**Last updated:** 2026-07-02

## Provider Map

### ASR Providers

| File | Purpose | Status |
|------|---------|--------|
| `qwen_asr.py` | **Primary provider** — Qwen ASR (http_cloud HTTP) | Active |
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

```
engine.py picks provider based on config.yaml:
  asr.provider: "qwen"       → qwen_asr.py
  asr.provider: "http"       → http_cloud.py (generic)
  refine.provider: "cloud"    → cloud_llm_refiner.py
  refine.provider: "llamacpp" → llamacpp_text_refiner.py
```

## Key Interfaces

```python
# ASR Provider interface (base.py)
class ASRProvider(ABC):
    async def recognize(self, audio_path: str, ...) -> ASRResult

# Text Refiner interface (base.py)
class TextRefiner(ABC):
    async def refine(self, text: str, ...) -> str
```

## Notes

- `asr_context.py` is NOT a provider — it's a context manager wrapping ASR calls
- Multiple refiners can be chained in `postprocess_pipeline.py`
- Provider credentials are set via environment variables, not hardcoded
