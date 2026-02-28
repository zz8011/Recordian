from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from shutil import which
import subprocess
import sys
import threading
import time
from typing import Callable


EventCallback = Callable[[dict[str, object]], None]


@dataclass(slots=True)
class WakeModelConfig:
    encoder: str
    decoder: str
    joiner: str
    tokens: str
    provider: str = "cpu"
    num_threads: int = 2
    sample_rate: int = 16000
    tokens_type: str = "ppinyin"
    keywords_file: str = ""


@dataclass(slots=True)
class WakeRuntimeConfig:
    enabled: bool
    prefixes: list[str]
    names: list[str]
    cooldown_s: float
    keyword_score: float
    keyword_threshold: float


def normalize_tokens_type(value: str) -> str:
    token = value.strip().lower()
    if token in {"char", "cjkchar"}:
        # Legacy value "char" maps to sherpa's "cjkchar".
        # For current Chinese KWS preset we prefer ppinyin by default.
        return "ppinyin"
    if token in {"ppinyin", "fpinyin", "bpe", "cjkchar+bpe", "phone+ppinyin"}:
        return token
    return "ppinyin"


def build_wake_phrases(prefixes: list[str], names: list[str]) -> list[str]:
    normalized_prefixes = [p.strip() for p in prefixes if p.strip()]
    normalized_names = [n.strip() for n in names if n.strip()]
    phrases: list[str] = []
    for prefix in normalized_prefixes:
        for name in normalized_names:
            phrase = f"{prefix}{name}"
            if phrase and phrase not in phrases:
                phrases.append(phrase)
    return phrases


def _normalize_list(value: object, *, fallback: list[str]) -> list[str]:
    if isinstance(value, str):
        items = [item.strip() for item in value.split(",") if item.strip()]
        return items or list(fallback)
    if isinstance(value, (list, tuple)):
        items = [str(item).strip() for item in value if str(item).strip()]
        return items or list(fallback)
    return list(fallback)


def ensure_keywords_file(
    *,
    phrases: list[str],
    tokens_path: Path,
    tokens_type: str,
    score: float,
    threshold: float,
    cache_dir: Path,
) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    raw_path = cache_dir / "keywords_raw.txt"
    out_path = cache_dir / "keywords.txt"

    raw_lines = [f"{phrase} @{phrase} :{score:.2f} #{threshold:.2f}" for phrase in phrases]
    raw_content = "\n".join(raw_lines) + "\n"

    def _is_cache_valid() -> bool:
        if not raw_path.exists() or not out_path.exists():
            return False
        try:
            if raw_path.read_text(encoding="utf-8") != raw_content:
                return False
            out_lines = [line.strip() for line in out_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except Exception:  # noqa: BLE001
            return False
        if len(out_lines) != len(raw_lines):
            return False
        return all(" @" in line and " :" in line and " #" in line for line in out_lines)

    if _is_cache_valid():
        return out_path

    raw_path.write_text(raw_content, encoding="utf-8")

    # Prefer python API: avoids PATH issues when app is launched by desktop entry.
    try:
        from sherpa_onnx.utils import text2token

        encoded = text2token(
            texts=phrases,
            tokens=str(tokens_path),
            tokens_type=tokens_type,
        )
        if len(encoded) == len(phrases):
            output_lines: list[str] = []
            for phrase, row in zip(phrases, encoded):
                token_items = [str(item).strip() for item in row if str(item).strip()]
                if not token_items:
                    raise RuntimeError("empty_token_row")
                output_lines.append(
                    f"{' '.join(token_items)} @{phrase} :{score:.2f} #{threshold:.2f}"
                )
            out_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
            if _is_cache_valid():
                return out_path
    except Exception:
        pass

    # Fallback to CLI for environments where python API is unavailable.
    cli_bin = which("sherpa-onnx-cli")
    if cli_bin is None:
        candidate = Path(sys.executable).parent / "sherpa-onnx-cli"
        if candidate.exists():
            cli_bin = str(candidate)

    if cli_bin is None:
        raise RuntimeError(
            "关键词转换失败：未找到 sherpa-onnx-cli，且 python API 不可用。"
            " 请安装 sherpa-onnx 和 pypinyin。"
        )

    cmd = [
        cli_bin,
        "text2token",
        "--tokens",
        str(tokens_path),
        "--tokens-type",
        tokens_type,
        str(raw_path),
        str(out_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not _is_cache_valid():
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "关键词转换失败，请检查 tokens_type 与依赖（ppinyin 需要 pypinyin）。"
            f" detail={detail}"
        )
    return out_path


class VoiceWakeService:
    def __init__(
        self,
        *,
        model: WakeModelConfig,
        runtime: WakeRuntimeConfig,
        on_wake: Callable[[str], None],
        on_event: EventCallback,
        cache_dir: Path,
    ) -> None:
        self.model = model
        self.runtime = runtime
        self.on_wake = on_wake
        self.on_event = on_event
        self.cache_dir = cache_dir

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_trigger = 0.0

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="recordian-voice-wake")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)

    def _emit(self, payload: dict[str, object]) -> None:
        payload.setdefault("event", "log")
        self.on_event(payload)

    def _check_model_files(self) -> None:
        required = [
            Path(self.model.encoder),
            Path(self.model.decoder),
            Path(self.model.joiner),
            Path(self.model.tokens),
        ]
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise FileNotFoundError(
                "语音唤醒模型文件缺失: " + ", ".join(missing)
            )

    def _resolve_keywords_file(self) -> Path:
        explicit = self.model.keywords_file.strip()
        if explicit:
            p = Path(explicit).expanduser()
            if not p.exists():
                raise FileNotFoundError(f"keywords_file 不存在: {p}")
            return p

        phrases = build_wake_phrases(self.runtime.prefixes, self.runtime.names)
        if not phrases:
            raise RuntimeError("唤醒词为空，请至少配置一个前缀和一个名字。")

        return ensure_keywords_file(
            phrases=phrases,
            tokens_path=Path(self.model.tokens),
            tokens_type=self.model.tokens_type,
            score=self.runtime.keyword_score,
            threshold=self.runtime.keyword_threshold,
            cache_dir=self.cache_dir,
        )

    def _run(self) -> None:
        try:
            import numpy as np
            import sherpa_onnx
            import sounddevice as sd
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_disabled: {type(exc).__name__}: {exc}"})
            return

        try:
            self._check_model_files()
            keywords_file = self._resolve_keywords_file()
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_setup_failed: {exc}"})
            return

        try:
            spotter = sherpa_onnx.KeywordSpotter(
                tokens=self.model.tokens,
                encoder=self.model.encoder,
                decoder=self.model.decoder,
                joiner=self.model.joiner,
                keywords_file=str(keywords_file),
                num_threads=self.model.num_threads,
                provider=self.model.provider,
            )
            stream = spotter.create_stream()
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_init_failed: {exc}"})
            return

        samples_per_read = int(self.model.sample_rate * 0.1)
        self._emit({"message": "voice_wake_ready"})
        try:
            with sd.InputStream(
                channels=1,
                samplerate=self.model.sample_rate,
                dtype="float32",
                blocksize=samples_per_read,
            ) as mic:
                while not self._stop.is_set():
                    audio, _ = mic.read(samples_per_read)
                    samples = np.ascontiguousarray(audio.reshape(-1))

                    stream.accept_waveform(self.model.sample_rate, samples)
                    while spotter.is_ready(stream):
                        spotter.decode_stream(stream)

                    result = spotter.get_result(stream).strip()
                    if not result:
                        continue

                    spotter.reset_stream(stream)
                    now = time.monotonic()
                    if now - self._last_trigger < max(0.0, self.runtime.cooldown_s):
                        self._emit({"message": f"voice_wake_ignored_in_cooldown: {result}"})
                        continue

                    self._last_trigger = now
                    self._emit({"event": "voice_wake_triggered", "keyword": result})
                    self.on_wake(result)
        except Exception as exc:  # noqa: BLE001
            self._emit({"message": f"voice_wake_runtime_error: {type(exc).__name__}: {exc}"})


def make_wake_model_config(args: argparse.Namespace) -> WakeModelConfig:
    return WakeModelConfig(
        encoder=str(getattr(args, "wake_encoder", "")),
        decoder=str(getattr(args, "wake_decoder", "")),
        joiner=str(getattr(args, "wake_joiner", "")),
        tokens=str(getattr(args, "wake_tokens", "")),
        provider=str(getattr(args, "wake_provider", "cpu")),
        num_threads=int(getattr(args, "wake_num_threads", 2)),
        sample_rate=int(getattr(args, "wake_sample_rate", 16000)),
        tokens_type=normalize_tokens_type(str(getattr(args, "wake_tokens_type", "ppinyin"))),
        keywords_file=str(getattr(args, "wake_keywords_file", "")),
    )


def make_wake_runtime_config(args: argparse.Namespace) -> WakeRuntimeConfig:
    prefixes = _normalize_list(getattr(args, "wake_prefix", ["嗨", "嘿"]), fallback=["嗨", "嘿"])
    names = _normalize_list(getattr(args, "wake_name", ["小二"]), fallback=["小二"])
    return WakeRuntimeConfig(
        enabled=bool(getattr(args, "enable_voice_wake", False)),
        prefixes=prefixes,
        names=names,
        cooldown_s=float(getattr(args, "wake_cooldown_s", 3.0)),
        keyword_score=float(getattr(args, "wake_keyword_score", 1.5)),
        keyword_threshold=float(getattr(args, "wake_keyword_threshold", 0.25)),
    )
